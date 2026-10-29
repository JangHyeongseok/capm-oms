"""Run the OMS submission demo in isolated databases: python -m oms.demo."""
import argparse
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from .api import create_app


def run_demo() -> dict:
    scenarios = []
    with TemporaryDirectory(prefix="oms-demo-") as directory:
        with TestClient(create_app(Path(directory) / "oms.sqlite3")) as client:
            def call(method, path, body=None, *, expected=200):
                response = client.request(method, path, json=body)
                if response.status_code != expected:
                    raise RuntimeError(f"{method} {path}: expected {expected}, got {response.status_code}: {response.text}")
                return response.json()

            request = {"client_order_id": "demo-buy-001", "symbol": "AAPL",
                       "quantity": 3, "limit_price": "100.25"}
            order = call("POST", "/orders", request, expected=201)
            order_url = "/orders/" + order["order_id"]
            repeated = call("POST", "/orders", request)
            assert order["status"] == "NEW" and repeated["order_id"] == order["order_id"]
            scenarios.append({"name": "order_idempotency", "result": "passed", "orders_created": 1})

            fill_url = "/simulation/broker-orders/demo-buy-001/fills"
            first_fill = {"execution_id": "demo-fill-001", "quantity": 1, "fill_price": "100.00"}
            broker = call("POST", fill_url, first_fill, expected=201)
            before = call("GET", order_url)
            assert broker["status"] == "PARTIALLY_FILLED" and before["status"] == "NEW"
            partial = call("POST", order_url + "/reconcile")
            repeated_fill = call("POST", fill_url, first_fill)
            assert Decimal(partial["filled_quantity"]) == Decimal(repeated_fill["filled_quantity"]) == 1
            assert Decimal(partial["remaining_quantity"]) == 2
            scenarios.append({"name": "partial_fill_and_execution_idempotency", "result": "passed",
                              "filled_quantity": 1, "remaining_quantity": 2})

            call("POST", fill_url, {"execution_id": "demo-fill-002", "quantity": 2,
                                    "fill_price": "99.50"}, expected=201)
            final = call("POST", order_url + "/reconcile")
            assert final["status"] == "FILLED" and Decimal(final["filled_quantity"]) == 3
            assert Decimal(final["remaining_quantity"]) == 0 and final["average_fill_price"] == "99.66666667"
            assert Decimal(final["total_fees"]) == Decimal("0.1495")
            assert len(call("GET", order_url + "/fills")) == 2
            scenarios.append({"name": "full_fill_and_weighted_average", "result": "passed",
                              "filled_quantity": 3, "average_fill_price": "99.66666667", "total_fees": "0.1495"})

            loss_request = {**request, "client_order_id": "demo-loss-001", "quantity": 1}
            pending = call("POST", "/simulation/orders/response-loss", loss_request, expected=202)
            broker = call("GET", "/simulation/broker-orders/demo-loss-001")
            assert pending["status"] == "PENDING_NEW" and broker["status"] == "NEW"
            loss_url = "/orders/" + pending["order_id"]
            recovered = call("POST", loss_url + "/reconcile")
            assert recovered["status"] == "NEW" and recovered["broker_order_id"] == broker["broker_order_id"]
            canceled = call("POST", loss_url + "/cancel")
            assert canceled["status"] == "CANCELED"
            scenarios.append({"name": "lost_response_reconciliation_and_cancel", "result": "passed",
                              "status_before_reconciliation": "PENDING_NEW", "final_status": "CANCELED"})

            rejected = call("POST", "/orders", {**request, "client_order_id": "demo-risk-001",
                                                "quantity": 101}, expected=201)
            assert rejected["status"] == "REJECTED" and rejected["broker_order_id"] is None
            scenarios.append({"name": "pre_trade_limit", "result": "passed", "status": "REJECTED"})

    return {"mode": "local simulation with isolated temporary databases",
            "result": "passed", "scenarios": scenarios}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Save the verified demo summary as JSON")
    args = parser.parse_args()
    result = json.dumps(run_demo(), ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result + "\n", encoding="utf-8")
    print(result)


if __name__ == "__main__":
    main()
