from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


class Status(StrEnum):
    PENDING_NEW = "PENDING_NEW"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class OrderRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{
            "client_order_id": "lesson-001", "symbol": "AAPL", "side": "buy",
            "order_type": "limit", "quantity": 3, "limit_price": "100.25",
        }]},
    )

    client_order_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    symbol: str = Field(min_length=1, max_length=15, pattern=r"^[A-Z][A-Z0-9.-]*$")
    side: Literal["buy", "sell"] = "buy"
    order_type: Literal["limit"] = "limit"
    quantity: int = Field(strict=True, gt=0, le=1_000_000)
    limit_price: Decimal = Field(gt=0, max_digits=10, decimal_places=2, examples=["100.25"])

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("limit_price", mode="before")
    @classmethod
    def require_decimal_string(cls, value: object) -> object:
        # JSON의 실수 대신 "100.25" 같은 문자열을 받는다.
        if not isinstance(value, (str, Decimal)):
            raise ValueError('limit_price must be a decimal string, e.g. "100.25"')
        return value

    @property
    def price_cents(self) -> int:
        return int(self.limit_price * 100)


ORDER_EXAMPLE = {
    "order_id": "b1f620c6-3b55-4a2f-919a-008d6c184971",
    "client_order_id": "lesson-001",
    "symbol": "AAPL",
    "side": "buy",
    "order_type": "limit",
    "currency": "USD",
    "quantity": 3,
    "filled_quantity": 0,
    "remaining_quantity": 3,
    "average_fill_price": None,
    "limit_price": "100.25",
    "status": "NEW",
    "broker_order_id": "sim-60b6974b-42e6-4a76-a1d2-541cb0155668",
    "reason": None,
    "created_at": "2026-10-28T09:00:00Z",
    "updated_at": "2026-10-28T09:00:00Z",
}


class FillProgress(BaseModel):
    quantity: int
    filled_quantity: int = 0
    # 금액 계산은 정수 센트로 수행하고, 내부 합계는 응답에서 제외한다.
    filled_notional_cents: int = Field(default=0, exclude=True)

    @computed_field
    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity

    @computed_field
    @property
    def average_fill_price(self) -> Decimal | None:
        if not self.filled_quantity:
            return None
        return (Decimal(self.filled_notional_cents) / self.filled_quantity / 100).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )


class FillRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{
            "execution_id": "fills-001-exec-001", "quantity": 1, "fill_price": "100.00",
        }]},
    )

    execution_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    quantity: int = Field(strict=True, gt=0, le=1_000_000)
    fill_price: Decimal = Field(gt=0, max_digits=10, decimal_places=2, examples=["100.00"])

    @field_validator("fill_price", mode="before")
    @classmethod
    def require_price_string(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError('fill_price must be a decimal string, e.g. "100.00"')
        return value


class Execution(FillRequest):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "execution_id": "fills-001-exec-001", "client_order_id": "fills-001", "quantity": 1,
        "fill_price": "100.00", "occurred_at": "2026-10-29T00:00:00Z",
    }]})

    client_order_id: str
    occurred_at: datetime


class Order(FillProgress):
    # 문서용 예시이며 실제 응답이나 DB의 초기값으로 사용하지 않는다.
    model_config = ConfigDict(json_schema_extra={"examples": [ORDER_EXAMPLE]})

    order_id: str
    client_order_id: str
    symbol: str
    side: Literal["buy", "sell"] = "buy"
    order_type: Literal["limit"] = "limit"
    currency: Literal["USD"] = "USD"
    quantity: int
    limit_price: Decimal = Field(examples=["100.25"])
    status: Status
    broker_order_id: str | None
    reason: str | None
    created_at: datetime
    updated_at: datetime


class OrderEvent(BaseModel):
    sequence: int
    from_status: Status | None
    to_status: Status
    reason: str | None
    occurred_at: datetime


class Account(BaseModel):
    currency: Literal["USD"] = "USD"
    initial_cash: Decimal
    cash: Decimal
    reserved_cash: Decimal
    positions: dict[str, int]
    reserved_positions: dict[str, int]

    @computed_field
    @property
    def available_cash(self) -> Decimal:
        return self.cash - self.reserved_cash


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass
