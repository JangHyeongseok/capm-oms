import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from oms.api import create_app
from oms.broker import BrokerUnavailable, SimBroker
from oms.domain import Conflict, FillRequest, Status
from oms.store import Store


def order_body(**changes):
    return {"client_order_id": "fills-001", "symbol": "AAPL", "quantity": 3,
            "limit_price": "100.25", **changes}


def fill_body(**changes):
    return {"execution_id": "fill-001", "quantity": 1, "fill_price": "100.00", **changes}


FILL_URL = "/simulation/broker-orders/fills-001/fills"


@pytest.fixture
def setup_order(tmp_path):
    path = tmp_path / "orders.sqlite3"
    with TestClient(create_app(path)) as client:
        order = client.post("/orders", json=order_body()).json()
        yield client, "/orders/" + order["order_id"], path


def test_partial_and_full_fill_are_imported_once_with_weighted_average(setup_order):
    client, url, _ = setup_order
    first = client.post(FILL_URL, json=fill_body())
    assert first.status_code == 201
    assert first.json()["status"] == "PARTIALLY_FILLED"
    assert first.json()["filled_quantity"] == 1
    assert first.json()["remaining_quantity"] == 2
    assert first.json()["average_fill_price"] == "100.0000"
    assert "filled_notional_cents" not in first.json()
    assert client.get(url).json()["status"] == "NEW"

    local = client.post(url + "/reconcile").json()
    assert local["status"] == "PARTIALLY_FILLED"
    assert local["quantity"] == 3
    assert local["filled_quantity"] == 1
    assert local["remaining_quantity"] == 2
    assert client.post(FILL_URL, json=fill_body()).status_code == 200
    assert client.post(url + "/reconcile").json() == local

    second_body = fill_body(execution_id="fill-002", quantity=2, fill_price="99.50")
    final = client.post(FILL_URL, json=second_body).json()
    assert final["status"] == "FILLED"
    local = client.post(url + "/reconcile").json()
    assert local["status"] == "FILLED"
    assert local["filled_quantity"] == 3
    assert local["remaining_quantity"] == 0
    assert local["average_fill_price"] == "99.6667"
    assert client.post(FILL_URL, json=second_body).status_code == 200
    assert client.post(url + "/reconcile").json() == local
    executions = client.get(url + "/fills").json()
    assert [fill["execution_id"] for fill in executions] == ["fill-001", "fill-002"]
    assert sum(fill["quantity"] for fill in executions) == 3
    assert [event["to_status"] for event in client.get(url + "/events").json()] == [
        "PENDING_NEW", "NEW", "PARTIALLY_FILLED", "FILLED"
    ]


@pytest.mark.parametrize("change", [
    {"quantity": 4}, {"fill_price": "100.26"},
])
def test_overfill_and_price_above_buy_limit_are_rejected_without_writes(setup_order, change):
    client, url, _ = setup_order
    assert client.post(FILL_URL, json=fill_body(**change)).status_code == 409
    broker = client.get("/simulation/broker-orders/fills-001").json()
    assert broker["status"] == "NEW"
    assert broker["executions"] == []
    assert client.get(url + "/fills").json() == []


@pytest.mark.parametrize("change", [
    {"quantity": 0}, {"quantity": True}, {"quantity": 1.5}, {"fill_price": 100.00},
    {"fill_price": "NaN"}, {"fill_price": "100.001"}, {"execution_id": ""},
])
def test_invalid_fill_never_changes_broker_state(setup_order, change):
    client, _, _ = setup_order
    assert client.post(FILL_URL, json=fill_body(**change)).status_code == 422
    assert client.get("/simulation/broker-orders/fills-001").json()["filled_quantity"] == 0


@pytest.mark.parametrize("change", [{"quantity": 2}, {"fill_price": "99.00"}])
def test_same_execution_id_with_different_details_is_conflict(setup_order, change):
    client, _, _ = setup_order
    client.post(FILL_URL, json=fill_body())
    assert client.post(FILL_URL, json=fill_body(**change)).status_code == 409
    assert client.get("/simulation/broker-orders/fills-001").json()["filled_quantity"] == 1


def test_execution_id_cannot_be_reused_on_a_different_order(setup_order):
    client, _, _ = setup_order
    client.post(FILL_URL, json=fill_body())
    client.post("/orders", json=order_body(client_order_id="fills-002"))
    result = client.post("/simulation/broker-orders/fills-002/fills", json=fill_body())
    assert result.status_code == 409
    assert client.get("/simulation/broker-orders/fills-002").json()["filled_quantity"] == 0


def test_partial_fill_survives_cancel_and_new_fills_are_blocked(setup_order):
    client, url, _ = setup_order
    client.post(FILL_URL, json=fill_body())
    client.post(url + "/reconcile")
    canceled = client.post(url + "/cancel").json()
    assert canceled["status"] == "CANCELED"
    assert canceled["filled_quantity"] == 1
    assert canceled["remaining_quantity"] == 2  # 미체결이지만 취소되어 더 이상 실행하지 않는다.
    assert canceled["average_fill_price"] == "100.0000"
    assert client.post(FILL_URL, json=fill_body(execution_id="fill-after-cancel")).status_code == 409
    assert client.post(FILL_URL, json=fill_body()).status_code == 200
    assert client.post(url + "/reconcile").json() == canceled


def test_full_fill_blocks_cancel_and_additional_fills(setup_order):
    client, url, _ = setup_order
    client.post(FILL_URL, json=fill_body(quantity=3))
    client.post(url + "/reconcile")
    assert client.post(url + "/cancel").status_code == 409
    assert client.post(FILL_URL, json=fill_body(execution_id="extra-fill")).status_code == 409
    assert client.get(url).json()["filled_quantity"] == 3


def test_cancel_imports_fills_that_happened_before_cancellation(tmp_path):
    class FillDuringCancel(SimBroker):
        def cancel(self, broker_order_id):
            self.fill("fills-001", FillRequest(**fill_body()))
            super().cancel(broker_order_id)

    broker = FillDuringCancel(tmp_path / "broker.sqlite3")
    with TestClient(create_app(tmp_path / "orders.sqlite3", broker)) as client:
        order = client.post("/orders", json=order_body()).json()
        url = "/orders/" + order["order_id"]
        result = client.post(url + "/cancel").json()
        assert result["status"] == "CANCELED"
        assert result["filled_quantity"] == 1
        assert len(client.get(url + "/fills").json()) == 1


def test_full_fill_before_cancel_is_reported_as_filled_not_canceled(setup_order):
    client, url, _ = setup_order
    client.post(FILL_URL, json=fill_body(quantity=3))
    # OMS는 아직 NEW지만 브로커는 FILLED다. 취소 거절 후 대사로 체결을 확인한다.
    response = client.post(url + "/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "FILLED"
    assert response.json()["filled_quantity"] == 3


def test_fills_are_imported_while_cancel_remains_unconfirmed(tmp_path):
    class CancelUnavailable(SimBroker):
        def cancel(self, broker_order_id):
            raise BrokerUnavailable()

    broker = CancelUnavailable(tmp_path / "broker.sqlite3")
    with TestClient(create_app(tmp_path / "orders.sqlite3", broker)) as client:
        order = client.post("/orders", json=order_body()).json()
        url = "/orders/" + order["order_id"]
        assert client.post(url + "/cancel").status_code == 202
        client.post(FILL_URL, json=fill_body())
        partial = client.post(url + "/reconcile")
        assert partial.status_code == 202
        assert partial.json()["status"] == "PENDING_CANCEL"
        assert partial.json()["filled_quantity"] == 1
        client.post(FILL_URL, json=fill_body(execution_id="fill-002", quantity=2))
        final = client.post(url + "/reconcile")
        assert final.status_code == 200
        assert final.json()["status"] == "FILLED"


def test_fills_and_averages_survive_restart(setup_order):
    client, url, path = setup_order
    client.post(FILL_URL, json=fill_body())
    partial = client.post(url + "/reconcile").json()
    with TestClient(create_app(path)) as restarted:
        assert restarted.get(url).json() == partial
        assert restarted.get("/simulation/broker-orders/fills-001").json()["filled_quantity"] == 1
        assert restarted.post(FILL_URL, json=fill_body()).status_code == 200
        assert restarted.post(url + "/reconcile").json() == partial


def test_concurrent_duplicate_fill_is_recorded_once(tmp_path):
    broker = SimBroker(tmp_path / "broker.sqlite3")
    with TestClient(create_app(tmp_path / "orders.sqlite3", broker)) as client:
        order = client.post("/orders", json=order_body()).json()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: broker.fill("fills-001", FillRequest(**fill_body())), range(16)))
        assert sum(created for _, created in results) == 1
        local = client.post("/orders/" + order["order_id"] + "/reconcile").json()
        assert local["filled_quantity"] == 1
        assert local["remaining_quantity"] == 2


def test_concurrent_distinct_fills_cannot_overfill(tmp_path):
    broker = SimBroker(tmp_path / "broker.sqlite3")
    with TestClient(create_app(tmp_path / "orders.sqlite3", broker)) as client:
        client.post("/orders", json=order_body(quantity=1))

        def fill(index):
            try:
                broker.fill("fills-001", FillRequest(**fill_body(execution_id=f"fill-{index}")))
                return True
            except Conflict:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(fill, range(2))) == 1
        assert broker.get_by_client_id("fills-001").filled_quantity == 1


def test_stale_snapshot_cannot_reduce_cumulative_fills(tmp_path):
    path = tmp_path / "orders.sqlite3"
    broker = SimBroker(tmp_path / "broker.sqlite3")
    with TestClient(create_app(path, broker)) as client:
        order = client.post("/orders", json=order_body()).json()
        url = "/orders/" + order["order_id"]
        stale, _ = broker.fill("fills-001", FillRequest(**fill_body()))
        client.post(url + "/reconcile")
        broker.fill("fills-001", FillRequest(**fill_body(execution_id="fill-002")))
        current = client.post(url + "/reconcile").json()
        with pytest.raises(Conflict):
            Store(path).apply_broker_snapshot(order["order_id"], Status.PARTIALLY_FILLED,
                                             stale, Status.PARTIALLY_FILLED, "stale snapshot")
        assert client.get(url).json() == current
        assert current["filled_quantity"] == 2


def test_inconsistent_snapshot_is_rejected_atomically(tmp_path):
    path = tmp_path / "orders.sqlite3"
    broker = SimBroker(tmp_path / "broker.sqlite3")
    with TestClient(create_app(path, broker)) as client:
        order = client.post("/orders", json=order_body()).json()
        snapshot, _ = broker.fill("fills-001", FillRequest(**fill_body()))
        inconsistent = snapshot.model_copy(update={"filled_quantity": 2})
        with pytest.raises(Conflict):
            Store(path).apply_broker_snapshot(order["order_id"], Status.NEW, inconsistent,
                                             Status.PARTIALLY_FILLED, "bad snapshot")
        assert client.get("/orders/" + order["order_id"]).json() == order
        assert client.get("/orders/" + order["order_id"] + "/fills").json() == []


def test_v2_broker_schema_migration_preserves_records_and_is_repeatable(tmp_path):
    path = tmp_path / "broker.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE broker_orders (
                client_order_id TEXT PRIMARY KEY, broker_order_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL, quantity INTEGER NOT NULL CHECK (quantity > 0),
                price_cents INTEGER NOT NULL CHECK (price_cents > 0),
                status TEXT NOT NULL CHECK (status IN ('NEW', 'CANCELED'))
            );
            INSERT INTO broker_orders VALUES ('old-new', 'sim-old-new', 'AAPL', 3, 10025, 'NEW');
            INSERT INTO broker_orders VALUES ('old-cancel', 'sim-old-cancel', 'AAPL', 1, 10025, 'CANCELED');
        """)
    broker = SimBroker(path)
    assert broker.get_by_client_id("old-new").broker_order_id == "sim-old-new"
    assert broker.get_by_client_id("old-cancel").status == Status.CANCELED
    broker.fill("old-new", FillRequest(**fill_body()))
    restarted = SimBroker(path)
    assert restarted.get_by_client_id("old-new").filled_quantity == 1
    assert restarted.get_by_client_id("old-new").average_fill_price == Decimal("100.0000")
    assert restarted.get_by_client_id("old-cancel").status == Status.CANCELED


def test_lookup_failure_preserves_last_confirmed_state(tmp_path):
    class LookupUnavailable(SimBroker):
        def get_by_client_id(self, client_order_id):
            raise BrokerUnavailable()

    broker = LookupUnavailable(tmp_path / "broker.sqlite3")
    with TestClient(create_app(tmp_path / "orders.sqlite3", broker)) as client:
        order = client.post("/orders", json=order_body()).json()
        url = "/orders/" + order["order_id"]
        assert client.post(url + "/reconcile").status_code == 503
        assert client.get(url).json() == order


def test_unknown_order_fill_and_execution_list(tmp_path):
    with TestClient(create_app(tmp_path / "orders.sqlite3")) as client:
        assert client.post("/simulation/broker-orders/missing/fills", json=fill_body()).status_code == 404
        assert client.get("/orders/missing/fills").status_code == 404


def test_simulation_endpoints_do_not_call_a_non_simulated_adapter(tmp_path):
    class OtherBroker:
        def submit(self, order):
            raise AssertionError("simulation must not submit to another adapter")

        def cancel(self, broker_order_id):
            raise AssertionError("simulation must not cancel through another adapter")

        def get_by_client_id(self, client_order_id):
            raise AssertionError("simulation must not query another adapter")

    with TestClient(create_app(tmp_path / "orders.sqlite3", OtherBroker())) as client:
        assert client.get("/health").json()["fills_enabled"] is False
        assert client.post(FILL_URL, json=fill_body()).status_code == 409
        assert client.post("/simulation/orders/response-loss", json=order_body()).status_code == 409
        assert client.get("/simulation/broker-orders/fills-001").status_code == 409
