from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class OrderItemIn(BaseModel):
    line_no: int | None = Field(default=None, ge=1, le=999)
    sku_code: str = Field(min_length=2, max_length=48)
    qty: int = Field(gt=0, le=999)
    unit_price: float = Field(gt=0, le=1_000_000)


class OrderDiscountIn(BaseModel):
    seq: int | None = Field(default=None, ge=1, le=99)
    kind: str = Field(default="other", pattern=r"^(platform_cross_store|shop_coupon|full_reduction|member|other)$")
    label: str | None = Field(default=None, max_length=120)
    amount: float = Field(ge=0, le=1_000_000)
    rule: dict = Field(default_factory=dict)


class RefundIn(BaseModel):
    event_no: str | None = Field(default=None, max_length=48)
    kind: str = Field(default="item", pattern=r"^(item|order|shipping)$")
    sku_code: str | None = Field(default=None, max_length=48)
    qty: int = Field(default=0, ge=0, le=999)
    occurred_at: str | None = Field(default=None, max_length=40)
    amount_declared: float | None = Field(default=None, ge=0, le=1_000_000)
    note: str | None = Field(default=None, max_length=1000)


class OrderIn(BaseModel):
    order_no: str = Field(min_length=3, max_length=48)
    competitor_id: int = Field(gt=0)
    paid_on: str | None = Field(default=None, max_length=40)
    channel: str = Field(default="competitor", max_length=24)
    campaign: str | None = Field(default=None, max_length=80)
    shipping_fee: float = Field(default=0.0, ge=0, le=100_000)
    payable_declared: float | None = Field(default=None, ge=0, le=10_000_000)
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)  # 单张订单货号上限
    discounts: list[OrderDiscountIn] = Field(default_factory=list, max_length=20)
    refunds: list[RefundIn] = Field(default_factory=list, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class RefundCreateIn(RefundIn):
    occurred_at: str = Field(max_length=40)


class OrderQuery(BaseModel):
    competitor_id: int | None = None
    reconcile_state: str | None = None
    q: str | None = None
    paid_on: date | None = None
