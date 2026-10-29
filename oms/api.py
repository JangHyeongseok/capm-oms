"""HTTP 요청을 주문 서비스에 전달하는 얇은 API 계층."""

import os
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .broker import Broker, BrokerOrder, BrokerUnavailable, LoseSubmitResponseBroker, SimBroker
from .domain import Conflict, Execution, FillRequest, NotFound, Order, OrderEvent, OrderRequest, Status
from .service import OrderService, RiskLimits
from .store import Store


def create_app(db_path: str | Path | None = None, broker: Broker | None = None, limits: RiskLimits | None = None) -> FastAPI:
    app = FastAPI(title="US Equity OMS", version="0.5.0", description="로컬 모의 주문·체결·상태 대사")
    default_path = Path(__file__).resolve().parents[1] / "data" / "oms.sqlite3"
    store = Store(db_path if db_path is not None else os.getenv("OMS_DB_PATH", str(default_path)))
    broker_path = store.path.with_name(f"{store.path.stem}.broker.sqlite3")
    active_broker = broker if broker is not None else SimBroker(broker_path)
    service = OrderService(store, active_broker, limits)
    loss_service = OrderService(store, LoseSubmitResponseBroker(active_broker), limits)

    @app.exception_handler(Conflict)
    async def conflict_handler(request: Request, exc: Conflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(NotFound)
    async def not_found_handler(request: Request, exc: NotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(BrokerUnavailable)
    async def broker_unavailable_handler(request: Request, exc: BrokerUnavailable):
        return JSONResponse(status_code=503, content={
            "detail": "Broker lookup unavailable; the last confirmed local record was preserved."
        })

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "local-simulation", "fills_enabled": isinstance(active_broker, SimBroker)}

    @app.post("/orders", response_model=Order, status_code=201, responses={
        200: {"description": "동일한 ID와 내용의 기존 주문"}, 202: {"description": "접수 결과 미확정"},
    })
    def submit_order(body: OrderRequest, response: Response):
        order, created = service.submit(body)
        if order.status in (Status.PENDING_NEW, Status.PENDING_CANCEL):
            response.status_code = 202
        elif not created:
            response.status_code = 200
        return order

    @app.get("/orders/{order_id}", response_model=Order)
    def get_order(order_id: str):
        return store.get(order_id)

    @app.get("/orders/{order_id}/events", response_model=list[OrderEvent])
    def get_events(order_id: str):
        return store.events(order_id)

    @app.get("/orders/{order_id}/fills", response_model=list[Execution])
    def get_order_fills(order_id: str):
        return store.executions(order_id)

    @app.post("/orders/{order_id}/cancel", response_model=Order, responses={202: {"description": "취소 결과 미확정"}})
    def cancel_order(order_id: str, response: Response):
        order = service.cancel(order_id)
        if order.status in (Status.PENDING_NEW, Status.PENDING_CANCEL):
            response.status_code = 202
        return order

    @app.post("/orders/{order_id}/reconcile", response_model=Order, responses={202: {"description": "상태 미확정"}})
    def reconcile_order(order_id: str, response: Response):
        order = service.reconcile(order_id)
        if order.status in (Status.PENDING_NEW, Status.PENDING_CANCEL):
            response.status_code = 202
        return order

    @app.get("/simulation/broker-orders/{client_order_id}", response_model=BrokerOrder)
    def get_broker_record(client_order_id: str):
        if not isinstance(active_broker, SimBroker):
            raise Conflict("simulation endpoints are only available with SimBroker")
        snapshot = active_broker.get_by_client_id(client_order_id)
        if snapshot is None:
            raise NotFound("no record in the simulated broker; this does not trigger resubmission")
        return snapshot

    @app.post("/simulation/broker-orders/{client_order_id}/fills", response_model=BrokerOrder, status_code=201,
              responses={200: {"description": "이미 기록된 동일 체결"}})
    def record_simulated_fill(client_order_id: str, body: FillRequest, response: Response):
        if not isinstance(active_broker, SimBroker):
            raise Conflict("simulation endpoints are only available with SimBroker")
        snapshot, created = active_broker.fill(client_order_id, body)
        if not created:
            response.status_code = 200
        return snapshot

    @app.post("/simulation/orders/response-loss", response_model=Order, status_code=202, responses={
        200: {"description": "기존 주문"}, 201: {"description": "한도 검사에서 거절된 새 주문"},
    })
    def simulate_response_loss(body: OrderRequest, response: Response):
        if not isinstance(active_broker, SimBroker):
            raise Conflict("simulation endpoints are only available with SimBroker")
        order, created = loss_service.submit(body)
        if order.status in (Status.PENDING_NEW, Status.PENDING_CANCEL):
            response.status_code = 202
        else:
            response.status_code = 201 if created else 200
        return order

    return app
