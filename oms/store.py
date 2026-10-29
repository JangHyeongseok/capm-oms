import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from .domain import Account, Conflict, Execution, NotFound, Order, OrderEvent, OrderRequest, Status

if TYPE_CHECKING:
    from .broker import BrokerOrder


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
                    status TEXT NOT NULL,
                    broker_order_id TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL REFERENCES orders(order_id),
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    reason TEXT,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_order
                    ON order_events(order_id, sequence);
                CREATE TABLE IF NOT EXISTS order_fills (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_id TEXT NOT NULL UNIQUE,
                    order_id TEXT NOT NULL REFERENCES orders(order_id),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS fills_by_order ON order_fills(order_id);
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    initial_cash_cents INTEGER NOT NULL CHECK (initial_cash_cents >= 0)
                );
                INSERT OR IGNORE INTO account VALUES (1, 10000000);
            """)
            # 기존 학습 DB의 기록을 유지한다. 과거 주문의 방향은 모두 매수였다.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(orders)")}
            if "side" not in columns:
                db.execute("ALTER TABLE orders ADD COLUMN side TEXT NOT NULL DEFAULT 'buy' CHECK (side IN ('buy', 'sell'))")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def order_from_row(self, db, row: sqlite3.Row) -> Order:
        fields = dict(row)
        fields["limit_price"] = (Decimal(fields.pop("price_cents")) / 100).quantize(Decimal("0.01"))
        totals = db.execute(
            """SELECT COALESCE(SUM(quantity), 0) AS filled_quantity,
               COALESCE(SUM(quantity * price_cents), 0) AS filled_notional_cents
               FROM order_fills WHERE order_id = ?""", (row["order_id"],)
        ).fetchone()
        fields.update(dict(totals))
        return Order(**fields)

    @staticmethod
    def account_from_db(db) -> Account:
        initial = db.execute("SELECT initial_cash_cents FROM account WHERE id = 1").fetchone()[0]
        cash, reserved_cash, positions, reserved_positions = initial, 0, {}, {}
        # 체결 기록에서 복원하므로 같은 체결을 다시 받아도 잔고가 중복 반영되지 않는다.
        for row in db.execute("""SELECT o.symbol, o.side, f.quantity, f.price_cents
                                 FROM order_fills f JOIN orders o USING (order_id)"""):
            sign = 1 if row["side"] == "buy" else -1
            positions[row["symbol"]] = positions.get(row["symbol"], 0) + sign * row["quantity"]
            cash -= sign * row["quantity"] * row["price_cents"]
        for row in db.execute("""SELECT o.*, o.quantity - COALESCE(SUM(f.quantity), 0) AS remaining
                                 FROM orders o LEFT JOIN order_fills f USING (order_id)
                                 WHERE o.status IN ('PENDING_NEW', 'NEW', 'PARTIALLY_FILLED', 'PENDING_CANCEL')
                                 GROUP BY o.order_id"""):
            if row["side"] == "buy":
                reserved_cash += row["remaining"] * row["price_cents"]
            else:
                reserved_positions[row["symbol"]] = reserved_positions.get(row["symbol"], 0) + row["remaining"]
        def money(cents):
            return (Decimal(cents) / 100).quantize(Decimal("0.01"))
        return Account(initial_cash=money(initial), cash=money(cash), reserved_cash=money(reserved_cash),
                       positions={s: q for s, q in sorted(positions.items()) if q},
                       reserved_positions={s: q for s, q in sorted(reserved_positions.items()) if q})

    def account(self) -> Account:
        with self.connection() as db:
            db.execute("BEGIN")
            return self.account_from_db(db)

    @staticmethod
    def check_transition(before: Status, after: Status):
        allowed = {
            Status.PENDING_NEW: {Status.NEW, Status.REJECTED, Status.CANCELED,
                                 Status.PARTIALLY_FILLED, Status.FILLED},
            Status.NEW: {Status.PENDING_CANCEL, Status.PARTIALLY_FILLED, Status.FILLED, Status.CANCELED},
            Status.PARTIALLY_FILLED: {Status.PENDING_CANCEL, Status.FILLED, Status.CANCELED},
            Status.PENDING_CANCEL: {Status.NEW, Status.PARTIALLY_FILLED, Status.FILLED, Status.CANCELED},
        }
        if before != after and after not in allowed.get(before, set()):
            raise Conflict(f"invalid transition: {before} -> {after}")

    @staticmethod
    def add_event(db, order_id, before, after, reason, now):
        db.execute(
            """INSERT INTO order_events
               (order_id, from_status, to_status, reason, occurred_at)
               VALUES (?, ?, ?, ?, ?)""",
            (order_id, before, after, reason, now),
        )

    def get(self, order_id: str) -> Order:
        with self.connection() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            if row is None:
                raise NotFound("order not found")
            return self.order_from_row(db, row)

    def create_once(self, request: OrderRequest, rejection: str | None) -> tuple[Order, bool]:
        with self.connection() as db:
            # 동시 요청도 한 번만 생성되도록 짧은 DB 트랜잭션에서 직렬화한다.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", (request.client_order_id,)
            ).fetchone()
            if row is not None:
                if (row["symbol"], row["side"], row["quantity"], row["price_cents"]) != (
                    request.symbol, request.side, request.quantity, request.price_cents
                ):
                    raise Conflict("client_order_id was already used with different order details")
                return self.order_from_row(db, row), False

            # 같은 트랜잭션에서 현금·주식 수량을 확인한다. 미체결 주문도 자원을 예약한다.
            account = self.account_from_db(db)
            if rejection is None and request.side == "buy" and request.quantity * request.limit_price > account.available_cash:
                rejection = "insufficient available cash"
            if rejection is None and request.side == "sell" and request.quantity > (
                account.positions.get(request.symbol, 0) - account.reserved_positions.get(request.symbol, 0)
            ):
                rejection = "insufficient available shares"
            now, order_id = utc_now(), str(uuid4())
            status = Status.REJECTED if rejection else Status.PENDING_NEW
            db.execute(
                """INSERT INTO orders
                   (order_id, client_order_id, symbol, side, quantity, price_cents,
                    status, reason, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, request.client_order_id, request.symbol, request.side, request.quantity,
                 request.price_cents, status, rejection, now, now),
            )
            self.add_event(db, order_id, None, status, rejection, now)
            row = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            return self.order_from_row(db, row), True
        # 이 메서드의 반환 시점에는 commit이 끝나 있다. 외부 호출은 그 이후에 한다.

    def transition(
        self, order_id: str, expected: Status, target: Status,
        *, broker_order_id: str | None = None, reason: str | None = None,
    ) -> Order:
        self.check_transition(expected, target)
        with self.connection() as db:
            now = utc_now()
            result = db.execute(
                """UPDATE orders SET status = ?, broker_order_id = COALESCE(?, broker_order_id),
                   reason = ?, updated_at = ? WHERE order_id = ? AND status = ?""",
                (target, broker_order_id, reason, now, order_id, expected),
            )
            if result.rowcount != 1:
                raise Conflict("order state changed; fetch the latest state")
            self.add_event(db, order_id, expected, target, reason, now)
            row = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            return self.order_from_row(db, row)

    def record_uncertainty(
        self, order_id: str, expected: Status,
        reason: str = "Broker response unknown; reconciliation required before retrying delivery.",
    ) -> Order:
        with self.connection() as db:
            now = utc_now()
            result = db.execute(
                "UPDATE orders SET reason = ?, updated_at = ? WHERE order_id = ? AND status = ?",
                (reason, now, order_id, expected),
            )
            if result.rowcount != 1:
                raise Conflict("order state changed; fetch the latest state")
            self.add_event(db, order_id, expected, expected, reason, now)
            row = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            return self.order_from_row(db, row)

    def apply_broker_snapshot(
        self, order_id: str, expected: Status, snapshot: "BrokerOrder",
        target: Status, reason: str,
    ) -> Order:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            if row is None:
                raise NotFound("order not found")
            order = self.order_from_row(db, row)
            if order.status != expected:
                raise Conflict("order state changed; fetch the latest state")
            local_identity = (order.client_order_id, order.symbol, order.side, order.order_type,
                              order.currency, order.quantity, order.limit_price)
            remote_identity = (snapshot.client_order_id, snapshot.symbol, snapshot.side, snapshot.order_type,
                               snapshot.currency, snapshot.quantity, snapshot.limit_price)
            if local_identity != remote_identity or (
                order.broker_order_id is not None and order.broker_order_id != snapshot.broker_order_id
            ):
                raise Conflict("broker record does not match the local order; manual review required")
            ids = {fill.execution_id for fill in snapshot.executions}
            if len(ids) != len(snapshot.executions):
                raise Conflict("broker snapshot contains duplicate execution IDs")
            existing = db.execute("SELECT * FROM order_fills WHERE order_id = ?", (order_id,)).fetchall()
            if not {fill["execution_id"] for fill in existing}.issubset(ids):
                raise Conflict("stale broker snapshot is missing previously recorded executions")

            filled = sum(fill.quantity for fill in snapshot.executions)
            notional = sum(fill.quantity * int(fill.fill_price * 100) for fill in snapshot.executions)
            if (filled != snapshot.filled_quantity or notional != snapshot.filled_notional_cents
                    or not 0 <= filled <= order.quantity):
                raise Conflict("broker execution totals are inconsistent")
            valid_status = {
                Status.NEW: filled == 0,
                Status.PARTIALLY_FILLED: 0 < filled < order.quantity,
                Status.FILLED: filled == order.quantity,
                Status.CANCELED: filled < order.quantity,
            }
            if not valid_status.get(snapshot.status, False):
                raise Conflict("broker status is inconsistent with executed quantity")
            if target != snapshot.status and not (
                target == Status.PENDING_CANCEL and snapshot.status in (Status.NEW, Status.PARTIALLY_FILLED)
            ):
                raise Conflict("target status does not match broker confirmation")
            self.check_transition(order.status, target)

            added = 0
            for fill in snapshot.executions:
                cents = int(fill.fill_price * 100)
                limit_violated = (fill.fill_price > order.limit_price if order.side == "buy"
                                  else fill.fill_price < order.limit_price)
                if (fill.client_order_id != order.client_order_id or fill.quantity <= 0
                        or fill.fill_price <= 0 or limit_violated
                        or fill.fill_price != Decimal(cents) / 100):
                    raise Conflict("broker execution does not match the order")
                previous = db.execute(
                    "SELECT * FROM order_fills WHERE execution_id = ?", (fill.execution_id,)
                ).fetchone()
                if previous is not None:
                    if (previous["order_id"], previous["quantity"], previous["price_cents"],
                            datetime.fromisoformat(previous["occurred_at"])) != (
                        order_id, fill.quantity, cents, fill.occurred_at
                    ):
                        raise Conflict("execution_id conflicts with a previously recorded fill")
                    continue
                db.execute(
                    """INSERT INTO order_fills
                       (execution_id, order_id, quantity, price_cents, occurred_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (fill.execution_id, order_id, fill.quantity, cents, fill.occurred_at.isoformat()),
                )
                added += 1

            changed = added or target != order.status or order.broker_order_id != snapshot.broker_order_id
            pending_note = order.status in (Status.PENDING_NEW, Status.PENDING_CANCEL) and reason != order.reason
            if not changed and not pending_note:
                return order
            now = utc_now()
            db.execute(
                "UPDATE orders SET status = ?, broker_order_id = ?, reason = ?, updated_at = ? WHERE order_id = ?",
                (target, snapshot.broker_order_id, reason, now, order_id),
            )
            self.add_event(db, order_id, order.status, target, reason, now)
            row = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            return self.order_from_row(db, row)

    def executions(self, order_id: str) -> list[Execution]:
        with self.connection() as db:
            db.execute("BEGIN")
            order = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            if order is None:
                raise NotFound("order not found")
            rows = db.execute("SELECT * FROM order_fills WHERE order_id = ? ORDER BY sequence", (order_id,)).fetchall()
            return [Execution(
                execution_id=fill["execution_id"], client_order_id=order["client_order_id"],
                quantity=fill["quantity"], fill_price=(Decimal(fill["price_cents"]) / 100).quantize(Decimal("0.01")),
                occurred_at=fill["occurred_at"],
            ) for fill in rows]

    def events(self, order_id: str) -> list[OrderEvent]:
        self.get(order_id)
        with self.connection() as db:
            rows = db.execute(
                """SELECT sequence, from_status, to_status, reason, occurred_at
                   FROM order_events WHERE order_id = ? ORDER BY sequence""", (order_id,)
            ).fetchall()
            return [OrderEvent(**dict(row)) for row in rows]
