import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import ConfigDict, Field

from .domain import Conflict, Execution, FillProgress, FillRequest, NotFound, Order, Status, fee_for
from .schema import TABLES, upgrade_legacy


class BrokerUnavailable(Exception):
    """응답이 없어 브로커의 처리 여부를 확정할 수 없다."""


class BrokerRejected(Exception):
    """브로커가 명시적으로 거절했다고 확인된 경우에만 사용한다."""


class BrokerOrder(FillProgress):
    """브로커가 보관하는 기록. OMS의 상태와 별도로 조회한다."""

    model_config = ConfigDict(json_schema_extra={"examples": [{
        "client_order_id": "reconcile-001",
        "broker_order_id": "sim-60b6974b-42e6-4a76-a1d2-541cb0155668",
        "symbol": "AAPL", "side": "buy", "order_type": "limit", "currency": "USD",
        "quantity": 1, "filled_quantity": 0, "remaining_quantity": 1,
        "average_fill_price": None, "executions": [],
        "limit_price": "100.25", "status": "NEW",
    }]})

    client_order_id: str
    broker_order_id: str
    symbol: str
    side: Literal["buy", "sell"] = "buy"
    order_type: Literal["limit"] = "limit"
    currency: Literal["USD"] = "USD"
    limit_price: Decimal = Field(examples=["100.25"])
    status: Literal[Status.NEW, Status.PARTIALLY_FILLED, Status.FILLED, Status.CANCELED]
    executions: list[Execution] = Field(default_factory=list)


class Broker(Protocol):
    def submit(self, order: Order) -> str: ...

    def cancel(self, broker_order_id: str) -> None: ...

    def get_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...


class SimBroker:
    """독립 DB에 접수/취소/수동 체결을 기록하는 로컬 브로커."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            upgrade_legacy(db, broker=True)
            for name in ("broker_orders", "broker_fills"):
                db.execute(TABLES[name])
            db.execute("CREATE INDEX IF NOT EXISTS broker_fills_by_order ON broker_fills(client_order_id)")

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

    @staticmethod
    def snapshot(db, row: sqlite3.Row) -> BrokerOrder:
        fields = dict(row)
        fills = db.execute(
            "SELECT * FROM broker_fills WHERE client_order_id = ? ORDER BY sequence",
            (row["client_order_id"],),
        ).fetchall()
        fields["executions"] = [Execution(
            execution_id=fill["execution_id"], client_order_id=fill["client_order_id"],
            quantity=fill["quantity"], fill_price=fill["fill_price"], fee=fill["fee"], occurred_at=fill["occurred_at"],
        ) for fill in fills]
        fields["filled_quantity"] = sum((Decimal(f["quantity"]) for f in fills), Decimal("0"))
        fields["filled_notional"] = sum((Decimal(f["quantity"]) * Decimal(f["fill_price"]) for f in fills), Decimal("0"))
        fields["total_fees"] = sum((Decimal(f["fee"]) for f in fills), Decimal("0"))
        return BrokerOrder(**fields)

    def submit(self, order: Order) -> str:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM broker_orders WHERE client_order_id = ?", (order.client_order_id,)
            ).fetchone()
            if row is not None:
                if (row["symbol"], row["side"], Decimal(row["quantity"]), Decimal(row["limit_price"])) != (
                    order.symbol, order.side, order.quantity, order.limit_price
                ):
                    raise BrokerRejected("broker client_order_id already has different order details")
                return row["broker_order_id"]
            broker_order_id = f"sim-{uuid4()}"
            db.execute(
                """INSERT INTO broker_orders
                   (client_order_id, broker_order_id, symbol, side, quantity, limit_price, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (order.client_order_id, broker_order_id, order.symbol, order.side,
                 str(order.quantity), str(order.limit_price), Status.NEW),
            )
            return broker_order_id
        # 호출자에게 반환하기 전에 브로커 자체의 트랜잭션을 commit한다.

    def cancel(self, broker_order_id: str) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM broker_orders WHERE broker_order_id = ?", (broker_order_id,)
            ).fetchone()
            if row is None:
                raise BrokerRejected("broker order not found; cancellation was not confirmed")
            if row["status"] == Status.FILLED:
                raise BrokerRejected("fully filled order cannot be canceled")
            result = db.execute(
                "UPDATE broker_orders SET status = ? WHERE broker_order_id = ?",
                (Status.CANCELED, broker_order_id),
            )
            if result.rowcount != 1:
                raise BrokerRejected("broker order not found; cancellation was not confirmed")

    def get_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        with self.connection() as db:
            db.execute("BEGIN")  # 주문 상태와 체결 목록을 같은 시점에서 읽는다.
            row = db.execute(
                "SELECT * FROM broker_orders WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
            return None if row is None else self.snapshot(db, row)

    def fill(self, client_order_id: str, request: FillRequest) -> tuple[BrokerOrder, bool]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM broker_orders WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
            if row is None:
                raise NotFound("broker order not found")
            previous = db.execute(
                "SELECT * FROM broker_fills WHERE execution_id = ?", (request.execution_id,)
            ).fetchone()
            if previous is not None:
                if (previous["client_order_id"], Decimal(previous["quantity"]), Decimal(previous["fill_price"])) != (
                    client_order_id, request.quantity, request.fill_price
                ):
                    raise Conflict("execution_id was already used with different fill details")
                return self.snapshot(db, row), False

            snapshot = self.snapshot(db, row)
            if snapshot.status not in (Status.NEW, Status.PARTIALLY_FILLED):
                raise Conflict(f"cannot fill an order in {snapshot.status}")
            if request.quantity > snapshot.remaining_quantity:
                raise Conflict("fill quantity exceeds the unfilled quantity")
            limit = Decimal(row["limit_price"])
            if ((row["side"] == "buy" and request.fill_price > limit)
                    or (row["side"] == "sell" and request.fill_price < limit)):
                raise Conflict("fill price violates the order limit price")
            db.execute(
                """INSERT INTO broker_fills
                   (execution_id, client_order_id, quantity, fill_price, fee, occurred_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (request.execution_id, client_order_id, str(request.quantity), str(request.fill_price),
                 str(fee_for(request.quantity, request.fill_price)), datetime.now(UTC).isoformat()),
            )
            target = (Status.FILLED if request.quantity == snapshot.remaining_quantity
                      else Status.PARTIALLY_FILLED)
            db.execute("UPDATE broker_orders SET status = ? WHERE client_order_id = ?",
                       (target, client_order_id))
            row = db.execute("SELECT * FROM broker_orders WHERE client_order_id = ?",
                             (client_order_id,)).fetchone()
            return self.snapshot(db, row), True


class LoseSubmitResponseBroker:
    """실습용: 실제 브로커 저장이 끝난 뒤 OMS로 돌아갈 응답만 유실시킨다."""

    def __init__(self, broker: Broker):
        self.broker = broker

    def submit(self, order: Order) -> str:
        self.broker.submit(order)
        raise BrokerUnavailable("simulated response loss after broker acceptance")

    def cancel(self, broker_order_id: str) -> None:
        self.broker.cancel(broker_order_id)

    def get_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return self.broker.get_by_client_id(client_order_id)
