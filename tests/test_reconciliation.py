import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from oms.api import create_app
from oms.broker import BrokerRejected, BrokerUnavailable, LoseSubmitResponseBroker, SimBroker
from oms.domain import Conflict, OrderRequest, Status
from oms.service import OrderService
from oms.store import Store


def payload(**changes):
    return {
        "client_order_id": "reconcile-001", "symbol": "AAPL", "quantity": 1,
        "limit_price": "100.25", **changes,
    }


def test_response_loss_and_reconciliation_without_resubmission(tmp_path):
    path = tmp_path / "orders.sqlite3"
    with TestClient(create_app(path)) as client:
        response = client.post("/simulation/orders/response-loss", json=payload())
        assert response.status_code == 202
        pending = response.json()
        assert pending["status"] == "PENDING_NEW"
        assert pending["broker_order_id"] is None
        snapshot = client.get("/simulation/broker-orders/reconcile-001").json()
        assert snapshot["status"] == "NEW"
        assert snapshot["limit_price"] == "100.25"
        assert client.get(f'/orders/{pending["order_id"]}').json()["status"] == "PENDING_NEW"
        assert client.post("/orders", json=payload()).status_code == 202

        url = f'/orders/{pending["order_id"]}/reconcile'
        confirmed = client.post(url)
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "NEW"
        assert confirmed.json()["broker_order_id"] == snapshot["broker_order_id"]
        assert client.post(url).json() == confirmed.json()
        events = client.get(f'/orders/{pending["order_id"]}/events').json()
        assert [event["to_status"] for event in events] == ["PENDING_NEW", "PENDING_NEW", "NEW"]

        # 대사는 접수 요청을 새로 만들지 않았으며, 실험 후 일반 접수도 정상이다.
        with sqlite3.connect(tmp_path / "orders.broker.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM broker_orders").fetchone()[0] == 1
        normal = client.post("/orders", json=payload(client_order_id="normal-001"))
        assert normal.status_code == 201
        assert normal.json()["status"] == "NEW"


def test_both_records_survive_application_restart(tmp_path):
    path = tmp_path / "orders.sqlite3"
    with TestClient(create_app(path)) as client:
        order = client.post("/simulation/orders/response-loss", json=payload()).json()
        before = client.get("/simulation/broker-orders/reconcile-001").json()
    with TestClient(create_app(path)) as restarted:
        assert restarted.get("/simulation/broker-orders/reconcile-001").json() == before
        result = restarted.post(f'/orders/{order["order_id"]}/reconcile')
        assert result.status_code == 200
        assert result.json()["broker_order_id"] == before["broker_order_id"]


def test_absent_broker_record_stays_pending_and_is_never_resent(tmp_path):
    class BeforeAcceptance(SimBroker):
        calls = 0

        def submit(self, order):
            self.calls += 1
            raise BrokerUnavailable()

    broker = BeforeAcceptance(tmp_path / "broker.sqlite3")
    with TestClient(create_app(tmp_path / "orders.sqlite3", broker)) as client:
        order = client.post("/orders", json=payload()).json()
        assert client.get("/simulation/broker-orders/reconcile-001").status_code == 404
        response = client.post(f'/orders/{order["order_id"]}/reconcile')
        assert response.status_code == 202
        assert response.json()["status"] == "PENDING_NEW"
        assert "no record" in response.json()["reason"]
        assert client.post("/orders", json=payload()).status_code == 202
        assert broker.calls == 1


def test_lookup_failure_keeps_pending_state(tmp_path):
    class LookupUnavailable(SimBroker):
        def get_by_client_id(self, client_order_id):
            raise BrokerUnavailable()

    broker = LookupUnavailable(tmp_path / "broker.sqlite3")
    with TestClient(create_app(tmp_path / "orders.sqlite3", broker)) as client:
        order = client.post("/simulation/orders/response-loss", json=payload()).json()
        response = client.post(f'/orders/{order["order_id"]}/reconcile')
        assert response.status_code == 202
        assert response.json()["status"] == "PENDING_NEW"


@pytest.mark.parametrize("change", [
    {"quantity": 2}, {"symbol": "MSFT"}, {"limit_price": Decimal("101.00")},
    {"client_order_id": "another-client-id"},
])
def test_mismatched_broker_record_does_not_change_local_state(tmp_path, change):
    class MismatchedRecord(SimBroker):
        def get_by_client_id(self, client_order_id):
            return super().get_by_client_id(client_order_id).model_copy(update=change)

    broker = MismatchedRecord(tmp_path / "broker.sqlite3")
    with TestClient(create_app(tmp_path / "orders.sqlite3", broker)) as client:
        order = client.post("/simulation/orders/response-loss", json=payload()).json()
        url = f'/orders/{order["order_id"]}'
        assert client.post(url + "/reconcile").status_code == 409
        assert client.get(url).json() == order


@pytest.mark.parametrize("cancel_was_applied", [False, True])
def test_cancel_reconciliation_requires_confirmation(tmp_path, cancel_was_applied):
    class CancelResponseLost(SimBroker):
        calls = 0

        def cancel(self, broker_order_id):
            self.calls += 1
            if cancel_was_applied:
                super().cancel(broker_order_id)
            raise BrokerUnavailable()

    broker = CancelResponseLost(tmp_path / "broker.sqlite3")
    with TestClient(create_app(tmp_path / "orders.sqlite3", broker)) as client:
        order = client.post("/orders", json=payload()).json()
        url = f'/orders/{order["order_id"]}'
        assert client.post(url + "/cancel").status_code == 202
        result = client.post(url + "/reconcile")
        assert result.status_code == (200 if cancel_was_applied else 202)
        assert result.json()["status"] == ("CANCELED" if cancel_was_applied else "PENDING_CANCEL")
        assert broker.calls == 1


def test_cancellation_already_confirmed_at_broker_after_lost_submit_response(tmp_path):
    broker = SimBroker(tmp_path / "broker.sqlite3")
    with TestClient(create_app(tmp_path / "orders.sqlite3", broker)) as client:
        order = client.post("/simulation/orders/response-loss", json=payload()).json()
        snapshot = broker.get_by_client_id("reconcile-001")
        broker.cancel(snapshot.broker_order_id)  # 브로커 쪽에서 이미 취소된 상황
        result = client.post(f'/orders/{order["order_id"]}/reconcile')
        assert result.status_code == 200
        assert result.json()["status"] == "CANCELED"
        assert result.json()["broker_order_id"] == snapshot.broker_order_id


def test_rejected_order_is_not_reconciled(tmp_path):
    class NoLookupExpected(SimBroker):
        def get_by_client_id(self, client_order_id):
            raise AssertionError("must not query the broker for a locally rejected order")

    broker = NoLookupExpected(tmp_path / "broker.sqlite3")
    with TestClient(create_app(tmp_path / "orders.sqlite3", broker)) as client:
        order = client.post("/orders", json=payload(quantity=101)).json()
        result = client.post(f'/orders/{order["order_id"]}/reconcile')
        assert result.status_code == 200
        assert result.json() == order


def test_broker_deduplicates_and_rejects_conflicting_intent(tmp_path):
    store = Store(tmp_path / "orders.sqlite3")
    order, _ = store.create_once(OrderRequest(**payload()), None)
    broker = SimBroker(tmp_path / "broker.sqlite3")
    broker_id = broker.submit(order)
    assert broker.submit(order) == broker_id
    with pytest.raises(BrokerRejected):
        broker.submit(order.model_copy(update={"quantity": 2}))
    assert broker.get_by_client_id(order.client_order_id).quantity == 1


def test_concurrent_reconciliation_records_only_one_confirmation(tmp_path):
    barrier = Barrier(2)

    class SynchronizedLookup(SimBroker):
        def get_by_client_id(self, client_order_id):
            snapshot = super().get_by_client_id(client_order_id)
            barrier.wait(timeout=5)
            return snapshot

    store = Store(tmp_path / "orders.sqlite3")
    broker = SynchronizedLookup(tmp_path / "broker.sqlite3")
    order, _ = OrderService(store, LoseSubmitResponseBroker(broker)).submit(OrderRequest(**payload()))
    service = OrderService(store, broker)

    def reconcile(_):
        try:
            return service.reconcile(order.order_id).status
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reconcile, range(2)))
    assert results.count(Status.NEW) == 1
    assert results.count("conflict") == 1
    assert [event.to_status for event in store.events(order.order_id)].count(Status.NEW) == 1


def test_simulation_validation_and_unknown_order(tmp_path):
    with TestClient(create_app(tmp_path / "orders.sqlite3")) as client:
        assert client.post("/orders/missing/reconcile").status_code == 404
        assert client.post("/simulation/orders/response-loss", json=payload(quantity=0)).status_code == 422
        result = client.post("/simulation/orders/response-loss", json=payload(quantity=101))
        assert result.status_code == 201
        assert result.json()["status"] == "REJECTED"
        assert client.get("/simulation/broker-orders/reconcile-001").status_code == 404
