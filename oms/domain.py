from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

QUANTITY_STEP = Decimal("0.00000001")
FEE_BPS = Decimal("5")
FEE_RATE = FEE_BPS / 10_000


def fee_for(quantity: Decimal, price: Decimal) -> Decimal:
    return quantity * price * FEE_RATE


class FractionalQuantity(BaseModel):
    quantity: Decimal = Field(gt=0, le=1_000_000, max_digits=15, decimal_places=8, examples=["1.25"])

    @field_validator("quantity", mode="before")
    @classmethod
    def require_exact_quantity(cls, value):
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise ValueError('quantity must be an integer or decimal string, e.g. "1.25"')
        return value


class Status(StrEnum):
    PENDING_NEW = "PENDING_NEW"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class OrderRequest(FractionalQuantity):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{
            "client_order_id": "lesson-001", "symbol": "AAPL", "side": "buy",
            "order_type": "limit", "quantity": "3.25", "limit_price": "100.25",
        }]},
    )

    client_order_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    symbol: str = Field(min_length=1, max_length=15, pattern=r"^[A-Z][A-Z0-9.-]*$")
    side: Literal["buy", "sell"] = "buy"
    order_type: Literal["limit"] = "limit"
    limit_price: Decimal = Field(gt=0, max_digits=16, decimal_places=8, examples=["100.25"])

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


ORDER_EXAMPLE = {
    "order_id": "b1f620c6-3b55-4a2f-919a-008d6c184971",
    "client_order_id": "lesson-001",
    "symbol": "AAPL",
    "side": "buy",
    "order_type": "limit",
    "currency": "USD",
    "quantity": "3.25",
    "filled_quantity": "0",
    "remaining_quantity": "3.25",
    "total_fees": "0",
    "average_fill_price": None,
    "limit_price": "100.25",
    "status": "NEW",
    "broker_order_id": "sim-60b6974b-42e6-4a76-a1d2-541cb0155668",
    "reason": None,
    "created_at": "2026-10-28T09:00:00Z",
    "updated_at": "2026-10-28T09:00:00Z",
}


class FillProgress(FractionalQuantity):
    filled_quantity: Decimal = Decimal("0")
    filled_notional: Decimal = Field(default=Decimal("0"), exclude=True)
    total_fees: Decimal = Decimal("0")

    @computed_field
    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @computed_field
    @property
    def average_fill_price(self) -> Decimal | None:
        if not self.filled_quantity:
            return None
        return (self.filled_notional / self.filled_quantity).quantize(
            Decimal("0.00000001"), rounding=ROUND_HALF_UP
        )


class FillRequest(FractionalQuantity):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{
            "execution_id": "fills-001-exec-001", "quantity": "0.5", "fill_price": "100.00",
        }]},
    )

    execution_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    fill_price: Decimal = Field(gt=0, max_digits=16, decimal_places=8, examples=["100.00"])

    @field_validator("fill_price", mode="before")
    @classmethod
    def require_price_string(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError('fill_price must be a decimal string, e.g. "100.00"')
        return value


class Execution(FillRequest):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "execution_id": "fills-001-exec-001", "client_order_id": "fills-001", "quantity": "0.5",
        "fill_price": "100.00", "fee": "0.025", "occurred_at": "2026-10-29T00:00:00Z",
    }]})

    client_order_id: str
    occurred_at: datetime
    fee: Decimal = Field(default=Decimal("0"), ge=0)


class Order(FillProgress):
    # 문서용 예시이며 실제 응답이나 DB의 초기값으로 사용하지 않는다.
    model_config = ConfigDict(json_schema_extra={"examples": [ORDER_EXAMPLE]})

    order_id: str
    client_order_id: str
    symbol: str
    side: Literal["buy", "sell"] = "buy"
    order_type: Literal["limit"] = "limit"
    currency: Literal["USD"] = "USD"
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
    total_fees: Decimal
    total_interest: Decimal = Decimal("0")
    as_of_date: date | None = None
    fee_bps: Decimal = FEE_BPS
    positions: dict[str, Decimal]
    reserved_positions: dict[str, Decimal]

    @computed_field
    @property
    def available_cash(self) -> Decimal:
        return self.cash - self.reserved_cash


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interest_rate: Decimal = Field(gt=-1, lt=1, examples=["0.0001"])

    @field_validator("interest_rate", mode="before")
    @classmethod
    def require_exact_rate(cls, value):
        if not isinstance(value, (str, Decimal)):
            raise ValueError("interest_rate must be a decimal string")
        return value


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass
