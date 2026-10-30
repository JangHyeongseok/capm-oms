"""API에서 주고받는 주문 데이터."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class OrderRequest(BaseModel):
    symbol: str = Field(min_length=1, examples=["AAPL"])
    quantity: int = Field(gt=0, strict=True, examples=[10])
    price: float = Field(gt=0, allow_inf_nan=False, examples=[100.0])
    kind: Literal["buy", "sell"]


class FillRequest(BaseModel):
    quantity: int = Field(gt=0, strict=True, examples=[3])


class Order(BaseModel):
    order_id: int
    symbol: str
    quantity: int
    price: float
    time: datetime
    kind: Literal["buy", "sell", "cancel"]
    status: Literal["submitted", "accepted", "partially_filled", "completed", "canceled"]
    filled_quantity: int
    original_order_id: int | None
