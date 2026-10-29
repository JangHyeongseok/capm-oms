from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from oms.api import create_app
from oms.broker import BrokerRejected, BrokerUnavailable, SimBroker
from oms.domain import OrderRequest, Status
from oms.service import OrderService
from oms.store import Store


class TrackingBroker(SimBroker):
    def __init__(self, path):
        super().__init__(path)
        self.submissions = []
        self.cancellations = []

    def submit(self, order):
        self.submissions.append(order.order_id)
        return super().submit(order)

    def cancel(self, broker_order_id):
        self.cancellations.append(broker_order_id)
        super().cancel(broker_order_id)


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "orders.sqlite3"
    broker = TrackingBroker(tmp_path / "broker.sqlite3")
    with TestClient(create_app(path, broker)) as client:
        yield client, broker, path


def payload(**changes):
    # 예시 가격이며 시세가 아니다.
    return {
        "client_order_id": "lesson-001", "symbol": "AAPL", "side": "buy",
        "order_type": "limit", "quantity": 3, "limit_price": "100.25", **changes,
    }


def test_submit_cancel_and_audit_trail(env):
    client, broker, _ = env
    created = client.post("/orders", json=payload())
    assert created.status_code == 201
    order = created.json()
    assert order["status"] == "NEW"  # 접수이지 체결이 아니다.
    assert order["limit_price"] == "100.25"
    assert order["broker_order_id"].startswith("sim-")
    assert client.get(f'/orders/{order["order_id"]}').json() == order

    url = f'/orders/{order["order_id"]}/cancel'
    assert client.post(url).json()["status"] == "CANCELED"
    assert client.post(url).json()["status"] == "CANCELED"
    assert len(broker.cancellations) == 1
    events = client.get(f'/orders/{order["order_id"]}/events').json()
    assert [event["to_status"] for event in events] == [
        "PENDING_NEW", "NEW", "PENDING_CANCEL", "CANCELED"
    ]


def test_replay_returns_existing_order_even_after_restart_and_cancel(env):
    client, broker, path = env
    first = client.post("/orders", json=payload()).json()
    client.post(f'/orders/{first["order_id"]}/cancel')
    restarted_broker = TrackingBroker(broker.path)
    with TestClient(create_app(path, restarted_broker)) as restarted:
        result = restarted.post("/orders", json=payload(symbol=" aapl ", limit_price="100.250"))
    assert result.status_code == 200
    assert result.json()["order_id"] == first["order_id"]
    assert result.json()["status"] == "CANCELED"
    assert len(broker.submissions) == 1
    assert restarted_broker.submissions == []


@pytest.mark.parametrize("change", [{"quantity": 4}, {"limit_price": "101.00"}, {"symbol": "MSFT"}])
def test_same_client_id_with_different_intent_is_conflict(env, change):
    client, broker, _ = env
    client.post("/orders", json=payload())
    assert client.post("/orders", json=payload(**change)).status_code == 409
    assert len(broker.submissions) == 1


@pytest.mark.parametrize("change", [{"quantity": 101}, {"quantity": 100, "limit_price": "100.01"}])
def test_risk_rejection_is_persisted_without_broker_call(env, change):
    client, broker, _ = env
    response = client.post("/orders", json=payload(**change))
    assert response.status_code == 201  # 거절 이력의 생성에 성공했다.
    order = response.json()
    assert order["status"] == "REJECTED"
    assert order["reason"]
    assert broker.submissions == []
    assert client.get(f'/orders/{order["order_id"]}').json()["status"] == "REJECTED"
    assert client.post(f'/orders/{order["order_id"]}/cancel').status_code == 409


@pytest.mark.parametrize("change", [
    {"quantity": 0}, {"quantity": 1.5}, {"quantity": True}, {"limit_price": 100.25},
    {"limit_price": "100.001"}, {"limit_price": "NaN"}, {"limit_price": "-1"},
    {"side": "sell"}, {"order_type": "market"}, {"time_in_force": "day"},
])
def test_invalid_or_unsupported_input_never_reaches_broker(env, change):
    client, broker, _ = env
    assert client.post("/orders", json=payload(**change)).status_code == 422
    assert broker.submissions == []


def test_order_is_committed_before_broker_call_and_timeout_does_not_resubmit(tmp_path):
    path = tmp_path / "orders.sqlite3"
    store = Store(path)

    class TimeoutAfterAcceptance(TrackingBroker):
        def submit(self, order):
            # 별도 연결로 읽힌다 = 외부 호출 전에 commit되었다.
            assert store.get(order.order_id).status == Status.PENDING_NEW
            super().submit(order)  # 브로커는 접수했지만 응답이 유실된 상황
            raise BrokerUnavailable()

    broker = TimeoutAfterAcceptance(tmp_path / "broker.sqlite3")
    with TestClient(create_app(path, broker)) as client:
        first = client.post("/orders", json=payload())
        assert first.status_code == 202
        order = first.json()
        assert order["status"] == "PENDING_NEW"
        assert "reconciliation" in order["reason"]
        assert client.post("/orders", json=payload()).status_code == 202
        assert client.post(f'/orders/{order["order_id"]}/cancel').status_code == 409
    with TestClient(create_app(path, broker)) as restarted:
        assert restarted.post("/orders", json=payload()).json()["order_id"] == order["order_id"]
    assert len(broker.submissions) == 1


def test_broker_explicit_rejection_is_distinct_from_timeout(tmp_path):
    class RejectingBroker(SimBroker):
        def submit(self, order):
            raise BrokerRejected("demo broker refused the order")

    with TestClient(create_app(tmp_path / "orders.sqlite3", RejectingBroker(tmp_path / "broker.sqlite3"))) as client:
        order = client.post("/orders", json=payload()).json()
        assert order["status"] == "REJECTED"
        assert order["reason"] == "demo broker refused the order"


def test_cancel_timeout_remains_pending_and_is_not_resent(tmp_path):
    class CancelTimeout(TrackingBroker):
        def cancel(self, broker_order_id):
            super().cancel(broker_order_id)
            raise BrokerUnavailable()

    broker = CancelTimeout(tmp_path / "broker.sqlite3")
    with TestClient(create_app(tmp_path / "orders.sqlite3", broker)) as client:
        order = client.post("/orders", json=payload()).json()
        url = f'/orders/{order["order_id"]}/cancel'
        assert client.post(url).status_code == 202
        assert client.post(url).json()["status"] == "PENDING_CANCEL"
        assert len(broker.cancellations) == 1


def test_explicit_cancel_rejection_returns_to_active_state(tmp_path):
    class CancelRejected(SimBroker):
        def cancel(self, broker_order_id):
            raise BrokerRejected("demo cancellation refused")

    with TestClient(create_app(tmp_path / "orders.sqlite3", CancelRejected(tmp_path / "broker.sqlite3"))) as client:
        order = client.post("/orders", json=payload()).json()
        canceled = client.post(f'/orders/{order["order_id"]}/cancel').json()
        assert canceled["status"] == "NEW"
        assert canceled["reason"] == "demo cancellation refused"


def test_concurrent_retries_create_and_deliver_only_one_order(tmp_path):
    store = Store(tmp_path / "orders.sqlite3")
    broker = TrackingBroker(tmp_path / "broker.sqlite3")
    service = OrderService(store, broker)
    request = OrderRequest(**payload())
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.submit(request), range(16)))
    assert len({order.order_id for order, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert len(broker.submissions) == 1
    assert store.get(results[0][0].order_id).status == Status.NEW


def test_unknown_order_and_health(env):
    client, _, _ = env
    assert client.get("/orders/missing").status_code == 404
    assert client.get("/orders/missing/events").status_code == 404
    assert client.post("/orders/missing/cancel").status_code == 404
    assert client.get("/health").json() == {
        "status": "ok", "mode": "local-simulation", "fills_enabled": True,
    }
