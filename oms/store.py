import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from .domain import Account, Conflict, Execution, NotFound, Order, OrderEvent, OrderRequest, Status, fee_for
from .schema import TABLES, upgrade_legacy

if TYPE_CHECKING:
    from .broker import BrokerOrder


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            upgrade_legacy(db)
            for name in ("orders", "order_fills", "account"):
                db.execute(TABLES[name])
            db.executescript("""
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
                CREATE INDEX IF NOT EXISTS fills_by_order ON order_fills(order_id);
                INSERT OR IGNORE INTO account VALUES (1, '100000.00');
                CREATE TABLE IF NOT EXISTS cash_accruals (
                    session_date TEXT PRIMARY KEY, rate TEXT NOT NULL, amount TEXT NOT NULL
                );
            """)

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
        fills = db.execute("SELECT * FROM order_fills WHERE order_id = ?", (row["order_id"],)).fetchall()
        fields["filled_quantity"] = sum((Decimal(f["quantity"]) for f in fills), Decimal("0"))
        fields["filled_notional"] = sum((Decimal(f["quantity"]) * Decimal(f["fill_price"]) for f in fills), Decimal("0"))
        fields["total_fees"] = sum((Decimal(f["fee"]) for f in fills), Decimal("0"))
        return Order(**fields)

    @staticmethod
    def account_from_db(db) -> Account:
        initial = Decimal(db.execute("SELECT initial_cash FROM account WHERE id = 1").fetchone()[0])
        cash, reserved_cash, total_fees = initial, Decimal("0"), Decimal("0")
        accruals = db.execute("SELECT * FROM cash_accruals ORDER BY session_date").fetchall()
        total_interest = sum((Decimal(row["amount"]) for row in accruals), Decimal("0"))
        cash += total_interest
        positions, reserved_positions, filled = {}, {}, {}
        # 체결 기록에서 복원하므로 같은 체결을 다시 받아도 잔고가 중복 반영되지 않는다.
        for row in db.execute("""SELECT o.order_id, o.symbol, o.side, f.quantity, f.fill_price, f.fee
                                 FROM order_fills f JOIN orders o USING (order_id)"""):
            sign = 1 if row["side"] == "buy" else -1
            quantity, price, fee = Decimal(row["quantity"]), Decimal(row["fill_price"]), Decimal(row["fee"])
            positions[row["symbol"]] = positions.get(row["symbol"], Decimal("0")) + sign * quantity
            filled[row["order_id"]] = filled.get(row["order_id"], Decimal("0")) + quantity
            cash -= sign * quantity * price + fee
            total_fees += fee
        for row in db.execute("""SELECT * FROM orders WHERE status IN
                                 ('PENDING_NEW', 'NEW', 'PARTIALLY_FILLED', 'PENDING_CANCEL')"""):
            remaining = Decimal(row["quantity"]) - filled.get(row["order_id"], Decimal("0"))
            if row["side"] == "buy":
                price = Decimal(row["limit_price"])
                reserved_cash += remaining * price + fee_for(remaining, price)
            else:
                reserved_positions[row["symbol"]] = reserved_positions.get(row["symbol"], Decimal("0")) + remaining
        return Account(initial_cash=initial, cash=cash, reserved_cash=reserved_cash, total_fees=total_fees,
                       total_interest=total_interest, as_of_date=accruals[-1]["session_date"] if accruals else None,
                       positions={s: q for s, q in sorted(positions.items()) if q},
                       reserved_positions={s: q for s, q in sorted(reserved_positions.items()) if q})

    def account(self) -> Account:
        with self.connection() as db:
            db.execute("BEGIN")
            return self.account_from_db(db)

    def begin_session(self, day: date, rate: Decimal) -> Account:
        """거래 전 현금에 일별 RF를 적용한다. 같은 거래일의 재요청은 한 번만 반영한다."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self.account_from_db(db)
            if account.as_of_date is not None and day < account.as_of_date:
                raise Conflict("cannot move the simulation clock backwards")
            previous = db.execute("SELECT * FROM cash_accruals WHERE session_date = ?", (day.isoformat(),)).fetchone()
            if previous is not None:
                if Decimal(previous["rate"]) != rate:
                    raise Conflict("session already exists with a different interest rate")
                return account
            if account.reserved_cash or account.reserved_positions:
                raise Conflict("reconcile or cancel pending orders before advancing the session")
            db.execute("INSERT INTO cash_accruals VALUES (?, ?, ?)",
                       (day.isoformat(), str(rate), str(account.cash * rate)))
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
                if (row["symbol"], row["side"], Decimal(row["quantity"]), Decimal(row["limit_price"])) != (
                    request.symbol, request.side, request.quantity, request.limit_price
                ):
                    raise Conflict("client_order_id was already used with different order details")
                return self.order_from_row(db, row), False

            # 같은 트랜잭션에서 현금·주식 수량을 확인한다. 미체결 주문도 자원을 예약한다.
            account = self.account_from_db(db)
            required_cash = request.quantity * request.limit_price + fee_for(request.quantity, request.limit_price)
            if rejection is None and request.side == "buy" and required_cash > account.available_cash:
                rejection = "insufficient available cash"
            if rejection is None and request.side == "sell" and request.quantity > (
                account.positions.get(request.symbol, 0) - account.reserved_positions.get(request.symbol, 0)
            ):
                rejection = "insufficient available shares"
            now, order_id = utc_now(), str(uuid4())
            status = Status.REJECTED if rejection else Status.PENDING_NEW
            db.execute(
                """INSERT INTO orders
                   (order_id, client_order_id, symbol, side, quantity, limit_price,
                    status, reason, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, request.client_order_id, request.symbol, request.side, str(request.quantity),
                 str(request.limit_price), status, rejection, now, now),
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

            filled = sum((fill.quantity for fill in snapshot.executions), Decimal("0"))
            notional = sum((fill.quantity * fill.fill_price for fill in snapshot.executions), Decimal("0"))
            fees = sum((fill.fee for fill in snapshot.executions), Decimal("0"))
            if (filled != snapshot.filled_quantity or notional != snapshot.filled_notional
                    or fees != snapshot.total_fees
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
                limit_violated = (fill.fill_price > order.limit_price if order.side == "buy"
                                  else fill.fill_price < order.limit_price)
                if (fill.client_order_id != order.client_order_id or fill.quantity <= 0
                        or fill.fill_price <= 0 or limit_violated):
                    raise Conflict("broker execution does not match the order")
                previous = db.execute(
                    "SELECT * FROM order_fills WHERE execution_id = ?", (fill.execution_id,)
                ).fetchone()
                if previous is not None:
                    if (previous["order_id"], Decimal(previous["quantity"]), Decimal(previous["fill_price"]), Decimal(previous["fee"]),
                            datetime.fromisoformat(previous["occurred_at"])) != (
                        order_id, fill.quantity, fill.fill_price, fill.fee, fill.occurred_at
                    ):
                        raise Conflict("execution_id conflicts with a previously recorded fill")
                    continue
                if fill.fee != fee_for(fill.quantity, fill.fill_price):
                    raise Conflict("broker execution fee is incorrect")
                db.execute(
                    """INSERT INTO order_fills
                       (execution_id, order_id, quantity, fill_price, fee, occurred_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (fill.execution_id, order_id, str(fill.quantity), str(fill.fill_price), str(fill.fee), fill.occurred_at.isoformat()),
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
            account = self.account_from_db(db)
            if account.cash < 0 or any(q < 0 for q in account.positions.values()):
                raise Conflict("broker executions would make the account negative")
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
                quantity=fill["quantity"], fill_price=fill["fill_price"], fee=fill["fee"], occurred_at=fill["occurred_at"],
            ) for fill in rows]

    def events(self, order_id: str) -> list[OrderEvent]:
        self.get(order_id)
        with self.connection() as db:
            rows = db.execute(
                """SELECT sequence, from_status, to_status, reason, occurred_at
                   FROM order_events WHERE order_id = ? ORDER BY sequence""", (order_id,)
            ).fetchall()
            return [OrderEvent(**dict(row)) for row in rows]
