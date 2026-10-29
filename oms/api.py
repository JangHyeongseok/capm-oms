import os
import json
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .broker import Broker, BrokerOrder, BrokerUnavailable, LoseSubmitResponseBroker, SimBroker
from .domain import ORDER_EXAMPLE, Conflict, Execution, FillRequest, NotFound, Order, OrderEvent, OrderRequest, Status
from .service import OrderService, RiskLimits
from .store import Store


def create_app(
    db_path: str | Path | None = None,
    broker: Broker | None = None,
    limits: RiskLimits | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Daily CAPM Portfolio & OMS Lab",
        version="0.4.0",
        description=(
            "일별 CAPM·평균분산 백테스트 보고서는 /research/ 에서 확인합니다. "
            "연구 원장과 별도로, 로컬 모의 OMS는 정수 주식 지정가 매수·수동 체결·대사를 지원합니다.\n\n"
            "화면의 Example Value는 문서용 예시입니다. Try it out → Execute를 누른 뒤 "
            "Server response 아래 Response body에서 실제 처리 결과를 확인하세요."
        ),
    )
    default_path = Path(__file__).resolve().parents[1] / "data" / "oms.sqlite3"
    store = Store(db_path if db_path is not None else os.getenv("OMS_DB_PATH", str(default_path)))
    broker_path = store.path.with_name(f"{store.path.stem}.broker.sqlite3")
    active_broker = broker if broker is not None else SimBroker(broker_path)
    service = OrderService(store, active_broker, limits)
    loss_service = OrderService(store, LoseSubmitResponseBroker(active_broker), limits)
    research_dir = Path(__file__).resolve().parents[1] / "artifacts" / "daily-capm"

    @app.get("/research", include_in_schema=False)
    @app.get("/research/", include_in_schema=False)
    def research_report():
        return RedirectResponse("/research/report.html")

    @app.get("/research/summary", tags=["백테스트"], summary="동봉된 고정 실험의 성과 조회")
    def research_summary():
        path = research_dir / "metrics.json"
        if not path.exists():
            raise NotFound("backtest results not found; run python -m research.run first")
        return json.loads(path.read_text())

    app.mount("/research", StaticFiles(directory=research_dir, check_dir=False), name="research")

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

    @app.post(
        "/orders", response_model=Order, status_code=201,
        responses={
            200: {"model": Order, "description": "같은 ID와 내용의 기존 주문"},
            201: {"description": "새 주문 기록 생성. 예시의 NEW는 모의 브로커 접수 상태"},
            202: {
                "model": Order, "description": "브로커 처리 여부 미확정",
                "content": {"application/json": {"example": {
                    **ORDER_EXAMPLE,
                    "status": "PENDING_NEW",
                    "broker_order_id": None,
                    "reason": "Broker response unknown; reconciliation required before retrying delivery.",
                }}},
            },
            409: {"description": "같은 ID에 다른 주문 내용"},
        },
    )
    def submit_order(body: OrderRequest, response: Response):
        order, created = service.submit(body)
        response.status_code = (
            202 if order.status in (Status.PENDING_NEW, Status.PENDING_CANCEL)
            else 201 if created else 200
        )
        return order

    @app.get("/orders/{order_id}", response_model=Order)
    def get_order(order_id: str):
        return store.get(order_id)

    @app.get("/orders/{order_id}/events", response_model=list[OrderEvent])
    def get_events(order_id: str):
        return store.events(order_id)

    @app.get("/orders/{order_id}/fills", response_model=list[Execution], summary="OMS에 반영된 체결 목록")
    def get_order_fills(order_id: str):
        return store.executions(order_id)

    @app.post(
        "/orders/{order_id}/cancel", response_model=Order,
        responses={
            200: {
                "description": "취소 처리 결과. status와 reason을 확인",
                "content": {"application/json": {"example": {
                    **ORDER_EXAMPLE, "status": "CANCELED",
                }}},
            },
            202: {
                "model": Order, "description": "취소 결과 미확정",
                "content": {"application/json": {"example": {
                    **ORDER_EXAMPLE, "status": "PENDING_CANCEL",
                    "reason": "Broker response unknown; reconciliation required before retrying delivery.",
                }}},
            },
            409: {"description": "취소할 수 없는 상태 또는 동시 상태 변경"},
        },
    )
    def cancel_order(order_id: str, response: Response):
        order = service.cancel(order_id)
        if order.status == Status.PENDING_CANCEL:
            response.status_code = 202
        return order

    @app.post(
        "/orders/{order_id}/reconcile", response_model=Order,
        summary="주문 상태와 체결 기록을 브로커와 대사",
        description="원래 client_order_id로 조회하고 새 체결 기록과 상태를 반영합니다. 새 주문은 보내지 않습니다.",
        responses={
            202: {
                "model": Order, "description": "기록 없음, 조회 실패 또는 취소 미확정",
                "content": {"application/json": {"example": {
                    **ORDER_EXAMPLE, "status": "PENDING_NEW", "broker_order_id": None,
                    "reason": "Broker lookup found no record; keeping order pending without resubmission.",
                }}},
            },
            409: {"description": "브로커 기록과 주문 내용 불일치 또는 동시 상태 변경"},
            503: {"description": "브로커 조회 실패. 마지막으로 확인된 로컬 기록 유지"},
        },
    )
    def reconcile_order(order_id: str, response: Response):
        order = service.reconcile(order_id)
        if order.status in (Status.PENDING_NEW, Status.PENDING_CANCEL):
            response.status_code = 202
        return order

    @app.get(
        "/simulation/broker-orders/{client_order_id}", response_model=BrokerOrder,
        tags=["모의 실험"], summary="모의 브로커 쪽 주문 기록 조회",
        responses={404: {"description": "모의 브로커에 기록이 없음"}},
    )
    def get_broker_record(client_order_id: str):
        if not isinstance(active_broker, SimBroker):
            raise Conflict("simulation endpoints are only available with SimBroker")
        snapshot = active_broker.get_by_client_id(client_order_id)
        if snapshot is None:
            raise NotFound("no record in the simulated broker; this does not trigger resubmission")
        return snapshot

    @app.post(
        "/simulation/broker-orders/{client_order_id}/fills", response_model=BrokerOrder, status_code=201,
        tags=["모의 실험"], summary="모의 브로커에 체결 한 건 기록",
        description=(
            "quantity는 이번 체결의 수량입니다. 같은 execution_id와 내용은 중복 반영하지 않습니다. "
            "OMS 반영은 POST /orders/{order_id}/reconcile로 수행합니다."
        ),
        responses={
            200: {"model": BrokerOrder, "description": "이미 기록한 체결의 재요청"},
            201: {"content": {"application/json": {"example": {
                "client_order_id": "fills-001",
                "broker_order_id": "sim-60b6974b-42e6-4a76-a1d2-541cb0155668",
                "symbol": "AAPL", "side": "buy", "order_type": "limit", "currency": "USD",
                "quantity": 3, "filled_quantity": 1, "remaining_quantity": 2,
                "limit_price": "100.25", "average_fill_price": "100.0000",
                "status": "PARTIALLY_FILLED", "executions": [{
                    "execution_id": "fills-001-exec-001", "client_order_id": "fills-001",
                    "quantity": 1, "fill_price": "100.00", "occurred_at": "2026-10-29T00:00:00Z",
                }],
            }}}},
            404: {"description": "브로커 주문 없음"},
            409: {"description": "초과 체결, 지정가 초과, 종료된 주문 또는 체결 ID 충돌"},
        },
    )
    def record_simulated_fill(client_order_id: str, body: FillRequest, response: Response):
        if not isinstance(active_broker, SimBroker):
            raise Conflict("simulation endpoints are only available with SimBroker")
        snapshot, created = active_broker.fill(client_order_id, body)
        response.status_code = 201 if created else 200
        return snapshot

    @app.post(
        "/simulation/orders/response-loss", response_model=Order, status_code=202,
        tags=["모의 실험"], summary="브로커 접수 후 응답 유실 재현",
        description=(
            "새 client_order_id로 실습하세요. 브로커 DB에 접수 기록을 commit한 뒤 "
            "응답 유실 예외를 발생시킵니다. 일반 POST /orders에는 영향을 주지 않습니다."
        ),
        responses={
            200: {"model": Order, "description": "이미 처리된 주문 기록"},
            201: {"model": Order, "description": "한도 검사에서 거절된 새 주문 기록"},
            202: {
                "description": "OMS는 미확정 상태, 모의 브로커에는 접수 기록 존재",
                "content": {"application/json": {"example": {
                    **ORDER_EXAMPLE, "client_order_id": "reconcile-001",
                    "status": "PENDING_NEW", "broker_order_id": None,
                    "reason": "Broker response unknown; reconciliation required before retrying delivery.",
                }}},
            },
        },
    )
    def simulate_response_loss(body: OrderRequest, response: Response):
        if not isinstance(active_broker, SimBroker):
            raise Conflict("simulation endpoints are only available with SimBroker")
        order, created = loss_service.submit(body)
        response.status_code = (
            202 if order.status in (Status.PENDING_NEW, Status.PENDING_CANCEL)
            else 201 if created else 200
        )
        return order

    return app
