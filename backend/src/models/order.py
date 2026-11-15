"""订单整单优惠分摊与回款变更：订单 / 货品组成 / 整单优惠 / 逐行分摊 / 退款事件。

系统原先把「整单跨店券」按行各抵一次，导致订单实付与逐行券后价越差越远。
这里把整单优惠独立建模：每笔优惠只记一次整单金额，再按各货号成交金额分摊到行，
逐行归属单独落表（order_allocations），可逐笔核对，不重复抵扣。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="RESTRICT"), index=True)
    paid_on: Mapped[datetime] = mapped_column(DateTime, index=True)  # 成交时间（业务时间，UTC naive）
    channel: Mapped[str] = mapped_column(String(24), default="competitor")  # competitor / own
    campaign: Mapped[str] = mapped_column(String(80), default="")
    items_total: Mapped[float] = mapped_column(Float, default=0.0)  # 商品成交金额（Σ行 单价×数量）
    discount_total: Mapped[float] = mapped_column(Float, default=0.0)  # 整单优惠合计（只算一次）
    shipping_fee: Mapped[float] = mapped_column(Float, default=0.0)
    payable_declared: Mapped[float] = mapped_column(Float, default=0.0)  # 平台回传的订单实付
    payable_after_refund: Mapped[float] = mapped_column(Float, default=0.0)  # 回冲后的剩余应付
    refunded_total: Mapped[float] = mapped_column(Float, default=0.0)  # 回冲合计
    status: Mapped[str] = mapped_column(String(24), default="paid", index=True)
    reconcile_state: Mapped[str] = mapped_column(String(16), default="failed", index=True)
    reconcile_checks: Mapped[list] = mapped_column(JSON, default=list)
    settle: Mapped[dict] = mapped_column(JSON, default=dict)  # 引擎完整结算结果（快照留证）
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (UniqueConstraint("order_id", "line_no", name="uq_order_items_line"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    line_no: Mapped[int] = mapped_column(Integer, default=1)
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id", ondelete="RESTRICT"), index=True)
    sku_code: Mapped[str] = mapped_column(String(48), default="")
    qty: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)  # 成交单价（分摊前）
    gross_amount: Mapped[float] = mapped_column(Float, default=0.0)  # 成交金额 = 单价 × 数量
    discount_allocated: Mapped[float] = mapped_column(Float, default=0.0)  # 整单优惠分摊到本行合计
    net_amount: Mapped[float] = mapped_column(Float, default=0.0)  # 券后净额 = 成交金额 − 分摊
    effective_unit_price: Mapped[float] = mapped_column(Float, default=0.0)  # 券后单价（分摊后）
    refunded_qty: Mapped[int] = mapped_column(Integer, default=0)
    refunded_amount: Mapped[float] = mapped_column(Float, default=0.0)
    remaining_qty: Mapped[int] = mapped_column(Integer, default=0)
    remaining_net: Mapped[float] = mapped_column(Float, default=0.0)
    legacy_discount: Mapped[float] = mapped_column(Float, default=0.0)  # 旧口径：本行被全额抵扣的整单优惠（仅留证对比）
    legacy_net: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OrderDiscount(Base):
    __tablename__ = "order_discounts"
    __table_args__ = (UniqueConstraint("order_id", "seq", name="uq_order_discounts_seq"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=1)  # 业务叠加顺序
    kind: Mapped[str] = mapped_column(String(32), default="other", index=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    amount: Mapped[float] = mapped_column(Float, default=0.0)  # 整单只记一次
    rule: Mapped[dict] = mapped_column(JSON, default=dict)  # 门槛/每满/张数等原始配置
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OrderAllocation(Base):
    """逐行优惠归属：每笔整单优惠 × 每个货号 = 一条分摊记录（可逐笔核对）。"""

    __tablename__ = "order_allocations"
    __table_args__ = (UniqueConstraint("discount_id", "item_id", name="uq_allocation_discount_item"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    discount_id: Mapped[int] = mapped_column(ForeignKey("order_discounts.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=1)
    weight: Mapped[float] = mapped_column(Float, default=0.0)  # 分摊权重 = 该行成交金额
    amount: Mapped[float] = mapped_column(Float, default=0.0)  # 分摊到该行的金额
    refunded_amount: Mapped[float] = mapped_column(Float, default=0.0)  # 已回冲金额
    remaining_amount: Mapped[float] = mapped_column(Float, default=0.0)  # 未回冲金额
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RefundEvent(Base):
    """回款变更（退款事件）：按业务发生时间回放，晚到事件也要按发生顺序回冲。"""

    __tablename__ = "refund_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    event_no: Mapped[str] = mapped_column(String(48), default="")
    arrival_seq: Mapped[int] = mapped_column(Integer, default=1)  # 到达顺序（入库顺序）
    business_seq: Mapped[int] = mapped_column(Integer, default=1)  # 回放后的业务顺序
    kind: Mapped[str] = mapped_column(String(16), default="item", index=True)
    sku_id: Mapped[int | None] = mapped_column(ForeignKey("skus.id", ondelete="SET NULL"), nullable=True)
    sku_code: Mapped[str] = mapped_column(String(48), default="")
    qty: Mapped[int] = mapped_column(Integer, default=0)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    amount_declared: Mapped[float | None] = mapped_column(Float, nullable=True)  # 回款流水里的实退金额
    amount_reversed: Mapped[float] = mapped_column(Float, default=0.0)  # 按分摊口径计算出的回冲金额
    variance: Mapped[float] = mapped_column(Float, default=0.0)
    state: Mapped[str] = mapped_column(String(12), default="applied", index=True)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reversal: Mapped[dict] = mapped_column(JSON, default=dict)  # 逐行/逐笔回冲明细
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
