"""HTTP 요청을 받아 주문을 생성·조회·접수·체결·취소한다."""

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .domain import FillRequest, Order, OrderRequest
from .store import Store


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Simple OMS", version="1.0.0")
    default_path = Path(__file__).resolve().parents[1] / "data" / "simple-oms.sqlite3"
    store = Store(db_path if db_path is not None else os.getenv("OMS_DB_PATH", str(default_path)))

    @app.exception_handler(LookupError)
    async def not_found_handler(request: Request, exc: LookupError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def invalid_order_handler(request: Request, exc: ValueError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/orders", response_model=Order, status_code=201)
    def create_order(body: OrderRequest):
        return store.create(body)

    @app.get("/orders", response_model=list[Order])
    def list_orders():
        return store.list_orders()

    @app.get("/orders/{order_id}", response_model=Order)
    def get_order(order_id: int):
        return store.get(order_id)

    @app.post("/orders/{order_id}/accept", response_model=Order)
    def accept_order(order_id: int):
        return store.accept(order_id)

    @app.post("/orders/{order_id}/fills", response_model=Order)
    def fill_order(order_id: int, body: FillRequest):
        return store.fill(order_id, body)

    @app.post("/orders/{order_id}/cancel", response_model=Order, status_code=201)
    def cancel_order(order_id: int):
        return store.cancel(order_id)

    return app
