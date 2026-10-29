from dataclasses import dataclass
from decimal import Decimal

from .broker import Broker, BrokerRejected, BrokerUnavailable
from .domain import Conflict, Order, OrderRequest, Status
from .store import Store


@dataclass(frozen=True)
class RiskLimits:
    # 학습용 주문당 상한. 계좌의 현금/매수가능금액을 의미하지 않는다.
    max_quantity: int = 100
    max_notional: Decimal = Decimal("10000.00")

    def rejection(self, request: OrderRequest) -> str | None:
        if request.quantity > self.max_quantity:
            return f"quantity exceeds demo limit of {self.max_quantity} shares"
        if request.quantity * request.limit_price > self.max_notional:
            return f"notional exceeds demo limit of USD {self.max_notional}"
        return None


class OrderService:
    def __init__(self, store: Store, broker: Broker, limits: RiskLimits | None = None):
        self.store, self.broker = store, broker
        self.limits = limits or RiskLimits()

    def submit(self, request: OrderRequest) -> tuple[Order, bool]:
        order, created = self.store.create_once(request, self.limits.rejection(request))
        if not created or order.status == Status.REJECTED:
            return order, created

        # 주문 의도를 먼저 commit한 뒤 브로커를 호출한다. DB 잠금은 잡고 있지 않다.
        try:
            broker_order_id = self.broker.submit(order)
        except BrokerUnavailable:
            return self.store.record_uncertainty(order.order_id, Status.PENDING_NEW), True
        except BrokerRejected as exc:
            return self.store.transition(
                order.order_id, Status.PENDING_NEW, Status.REJECTED, reason=str(exc)
            ), True
        return self.store.transition(
            order.order_id, Status.PENDING_NEW, Status.NEW, broker_order_id=broker_order_id
        ), True

    def cancel(self, order_id: str) -> Order:
        order = self.store.get(order_id)
        if order.status in (Status.CANCELED, Status.PENDING_CANCEL):
            return order
        if order.status not in (Status.NEW, Status.PARTIALLY_FILLED):
            raise Conflict(f"cannot cancel an order in {order.status}")

        # 조건부 UPDATE의 승자만 취소를 전송한다.
        order = self.store.transition(order_id, order.status, Status.PENDING_CANCEL)
        assert order.broker_order_id is not None
        try:
            self.broker.cancel(order.broker_order_id)
        except BrokerUnavailable:
            return self.store.record_uncertainty(order_id, Status.PENDING_CANCEL)
        except BrokerRejected as exc:
            return self.reconcile(order_id, cancel_rejection=str(exc))
        return self.reconcile(order_id)

    def reconcile(self, order_id: str, *, cancel_rejection: str | None = None) -> Order:
        order = self.store.get(order_id)
        if order.status in (Status.REJECTED, Status.CANCELED, Status.FILLED):
            return order

        # 외부 조회는 DB 잠금을 잡지 않고 수행한다. 새 주문을 보내지 않는다.
        try:
            snapshot = self.broker.get_by_client_id(order.client_order_id)
        except BrokerUnavailable:
            if order.status not in (Status.PENDING_NEW, Status.PENDING_CANCEL):
                raise  # 확인된 로컬 상태는 보존하고 API에서 조회 실패를 알린다.
            return self.store.record_uncertainty(order_id, order.status)

        if snapshot is None:
            if order.status not in (Status.PENDING_NEW, Status.PENDING_CANCEL):
                raise Conflict("broker record is missing for a previously confirmed order")
            return self.store.record_uncertainty(
                order_id, order.status,
                "Broker lookup found no record; keeping order pending without resubmission.",
            )

        target = snapshot.status
        reason = cancel_rejection or "Reconciled with the broker record; no order was resubmitted."
        if (order.status == Status.PENDING_CANCEL
                and snapshot.status in (Status.NEW, Status.PARTIALLY_FILLED)
                and cancel_rejection is None):
            # 취소 요청의 처리 중일 수 있다. NEW를 보고 취소 실패로 단정하지 않는다.
            target = Status.PENDING_CANCEL
            reason = "Broker still reports an active order; cancellation remains unconfirmed. No retry was sent."

        return self.store.apply_broker_snapshot(
            order_id, order.status, snapshot, target, reason,
        )
