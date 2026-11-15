"""订单服务：入库结算、逐行分摊落库、退款回放、对账与可比样本查询。

所有金额计算都委托给纯函数 order_allocation.settle_order；服务层只负责
「校验 -> 调用引擎 -> 写 orders / order_items / order_discounts / order_allocations /
refund_events」这一条链，保证页面能逐笔核对整单实付、逐行优惠归属与回冲记录。
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.config.settings import settings
from src.constants.enums import (
    ORDER_DISCOUNT_KIND_LABELS,
    ORDER_REFUND_KIND_LABELS,
    ORDER_STATUS_LABELS,
    RECONCILE_STATE_LABELS,
    REFUND_EVENT_STATE_LABELS,
    SampleBasis,
)
from src.models.competitor import Competitor
from src.models.order import (
    Order,
    OrderAllocation,
    OrderDiscount,
    OrderItem,
    RefundEvent,
)
from src.models.sku import Sku
from src.services import order_allocation
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc, local_now

COMPARABLE_WINDOW_DAYS = 3


def _as_local_naive(value) -> datetime:
    """把业务时间（无时区时按 UTC+8 解释）转成库里统一存储的 UTC naive。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date) and not isinstance(value, datetime):
        parsed = datetime(value.year, value.month, value.day)
    else:
        parsed = order_allocation._parse_time(value)
    if parsed.tzinfo is not None:

        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed - timedelta(minutes=_offset())


def _display_time(value: datetime | None) -> str | None:
    """库里是 UTC naive，展示时按业务时区（UTC+8）返回带时区的 ISO。"""
    if value is None:
        return None
    return (value + timedelta(minutes=_offset())).isoformat(timespec="seconds") + "+08:00"


def _engine_payload(items: list, discounts: list, refunds: list, shipping_fee, payable_declared) -> dict:
    return order_allocation.settle_order(
        shipping_fee=shipping_fee or 0,
        payable_declared=payable_declared,
        items=items,
        discounts=discounts,
        refunds=refunds,
    )


def _stored_refund_inputs(db: Session, order: Order) -> list[dict]:
    rows = db.scalars(
        select(RefundEvent)
        .where(RefundEvent.order_id == order.id)
        .order_by(RefundEvent.arrival_seq, RefundEvent.id)
    ).all()
    return [
        {
            "event_no": row.event_no,
            "kind": row.kind,
            "sku_code": row.sku_code,
            "qty": row.qty,
            "occurred_at": row.occurred_at + timedelta(minutes=_offset()),
            "amount_declared": row.amount_declared,
            "arrival_seq": row.arrival_seq,
            "note": row.note,
        }
        for row in rows
    ]


def _stored_discount_inputs(db: Session, order: Order) -> list[dict]:
    rows = db.scalars(select(OrderDiscount).where(OrderDiscount.order_id == order.id).order_by(OrderDiscount.seq)).all()
    return [
        {"seq": row.seq, "kind": row.kind, "label": row.label, "amount": row.amount, "rule": row.rule or {}}
        for row in rows
    ]


def _stored_item_inputs(db: Session, order: Order) -> list[dict]:
    rows = db.scalars(select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.line_no)).all()
    return [
        {
            "line_no": row.line_no,
            "sku_id": row.sku_id,
            "sku_code": row.sku_code,
            "name": "",
            "qty": row.qty,
            "unit_price": row.unit_price,
            "gross_amount": row.gross_amount,
        }
        for row in rows
    ]


def _validate_refund(raw: dict) -> None:
    kind = str(raw.get("kind") or "item")
    if kind not in ("item", "order", "shipping"):
        raise AppError("VALIDATION", 422, {"field": "kind", "allowed": ["item", "order", "shipping"]})
    if kind == "item":
        if int(raw.get("qty") or 0) <= 0:
            raise AppError("VALIDATION", 422, {"field": "qty"})
        if not str(raw.get("sku_code") or "").strip():
            raise AppError("VALIDATION", 422, {"field": "sku_code"})
    if kind == "order" and int(raw.get("qty") or 0) not in (0,):
        raise AppError("VALIDATION", 422, {"field": "qty", "reason": "整单退款不带件数"})
    if not raw.get("occurred_at"):
        raise AppError("VALIDATION", 422, {"field": "occurred_at"})


def _guard_refund_replay(settle: dict) -> None:
    """参数化的退款事件必须当场可回放，否则 422；只有 resettle 允许带失败事件留证。"""
    invalid = [row for row in settle.get("refunds") or [] if row.get("state") == "invalid"]
    if invalid:
        raise AppError(
            "VALIDATION",
            422,
            {"field": "refunds", "errors": [{"event_no": row["event_no"], "reason": row["error"]} for row in invalid]},
        )


def _persist_settlement(db: Session, order: Order, settle: dict) -> None:
    """把引擎结果写回订单 + 货品 + 优惠 + 逐行分摊 + 退款事件（整单重建，幂等）。"""
    order.items_total = settle["totals"]["items_total"]
    order.discount_total = settle["totals"]["discount_total"]
    order.shipping_fee = settle["totals"]["shipping_fee"]
    order.payable_declared = settle["totals"]["payable_declared"]
    order.payable_after_refund = settle["totals"]["payable_after_refund"]
    order.refunded_total = settle["totals"]["refunded_total"]
    order.status = settle["status"]
    order.reconcile_state = settle["reconcile_state"]
    order.reconcile_checks = settle["checks"]
    order.settle = settle

    db.query(OrderAllocation).filter(OrderAllocation.order_id == order.id).delete(synchronize_session=False)
    db.query(OrderItem).filter(OrderItem.order_id == order.id).delete(synchronize_session=False)
    db.query(OrderDiscount).filter(OrderDiscount.order_id == order.id).delete(synchronize_session=False)
    db.flush()

    discount_ids: dict[int, int] = {}
    for discount in settle["discounts"]:
        row = OrderDiscount(
            order_id=order.id,
            seq=discount["seq"],
            kind=discount["kind"],
            label=discount["label"] or ORDER_DISCOUNT_KIND_LABELS.get(discount["kind"], ""),
            amount=discount["amount"],
            rule=discount["rule"] if isinstance(discount["rule"], dict) else {},
        )
        db.add(row)
        db.flush()
        discount_ids[discount["seq"]] = row.id

    item_ids: dict[int, int] = {}
    for item in settle["items"]:
        row = OrderItem(
            order_id=order.id,
            line_no=item["line_no"],
            sku_id=item["sku_id"],
            sku_code=item["sku_code"],
            qty=item["qty"],
            unit_price=item["unit_price"],
            gross_amount=item["gross_amount"],
            discount_allocated=item["discount_allocated"],
            net_amount=item["net_amount"],
            effective_unit_price=item["effective_unit_price"],
            refunded_qty=item["refunded_qty"],
            refunded_amount=item["refunded_amount"],
            remaining_qty=item["remaining_qty"],
            remaining_net=item["remaining_net"],
            legacy_discount=item["legacy_discount"],
            legacy_net=item["legacy_net"],
        )
        db.add(row)
        db.flush()
        item_ids[item["line_no"]] = row.id

    for discount in settle["discounts"]:
        for alloc in discount["allocations"]:
            db.add(
                OrderAllocation(
                    order_id=order.id,
                    discount_id=discount_ids[discount["seq"]],
                    item_id=item_ids[alloc["line_no"]],
                    seq=discount["seq"],
                    weight=alloc["weight"],
                    amount=alloc["amount"],
                    refunded_amount=alloc["refunded"],
                    remaining_amount=alloc["remaining"],
                )
            )
    db.flush()


def _persist_refunds(db: Session, order: Order, settle: dict) -> None:
    existing = {row.event_no: row for row in db.scalars(select(RefundEvent).where(RefundEvent.order_id == order.id)).all()}
    for refund in settle["refunds"]:
        row = existing.pop(refund["event_no"], None)
        if row is None:
            row = RefundEvent(order_id=order.id, event_no=refund["event_no"])
            db.add(row)
        row.arrival_seq = refund["arrival_seq"]
        row.business_seq = refund["business_seq"]
        row.kind = refund["kind"]
        row.sku_code = refund["sku_code"]
        row.qty = refund["qty"]
        row.occurred_at = _as_local_naive(order_allocation._parse_time(refund["occurred_at"]))
        row.amount_declared = refund["amount_declared"]
        row.amount_reversed = refund["amount_reversed"]
        row.variance = refund["variance"]
        row.state = refund["state"]
        row.error = refund["error"]
        row.reversal = refund["reversal"]
        row.note = refund["note"]
    for leftover in existing.values():
        db.delete(leftover)
    db.flush()


def create_order(db: Session, payload: dict) -> Order:
    order_no = str(payload.get("order_no") or "").strip()
    if not order_no:
        raise AppError("VALIDATION", 422, {"field": "order_no"})
    if db.scalar(select(Order).where(Order.order_no == order_no)) is not None:
        raise AppError("CONFLICT", 409, {"order_no": order_no})
    competitor_id = int(payload.get("competitor_id") or 0)
    if db.get(Competitor, competitor_id) is None:
        raise AppError("NOT_FOUND", 404, {"entity": "competitor", "id": competitor_id})
    raw_items = payload.get("items") or []
    if not raw_items:
        raise AppError("VALIDATION", 422, {"field": "items"})

    # 统一用 SKU 编码作为货号归属，并把 sku_id 解析出来
    sku_map = {row.sku_code: row for row in db.scalars(select(Sku)).all()}
    items: list[dict] = []
    for raw in raw_items:
        sku_code = str(raw.get("sku_code") or "").strip()
        sku = sku_map.get(sku_code)
        if sku is None:
            raise AppError("VALIDATION", 422, {"field": "items.sku_code", "reason": f"未知货号 {sku_code}"})
        items.append(
            {
                "line_no": raw.get("line_no"),
                "sku_id": sku.id,
                "sku_code": sku.sku_code,
                "name": sku.name,
                "qty": raw.get("qty"),
                "unit_price": raw.get("unit_price"),
            }
        )
    raw_discounts = payload.get("discounts") or []
    refunds = payload.get("refunds") or []
    for refund in refunds:
        _validate_refund(refund)
    try:
        settle = _engine_payload(items, raw_discounts, refunds, payload.get("shipping_fee"), payload.get("payable_declared"))
    except ValueError as exc:
        raise AppError("VALIDATION", 422, {"field": "order", "reason": str(exc)}) from exc
    _guard_refund_replay(settle)

    order = Order(
        order_no=order_no,
        competitor_id=competitor_id,
        paid_on=_as_local_naive(payload.get("paid_on") or local_now()),
        channel=str(payload.get("channel") or "competitor"),
        campaign=str(payload.get("campaign") or ""),
        note=payload.get("note"),
    )
    db.add(order)
    db.flush()
    _persist_settlement(db, order, settle)
    _persist_refunds(db, order, settle)
    db.flush()
    return order


def add_refund(db: Session, order: Order, payload: dict) -> Order:
    """追加一条回款变更（可能是晚到事件），整单按业务发生时间重放回冲。"""
    _validate_refund(payload)
    refunds = _stored_refund_inputs(db, order)
    arrival_seq = max((row["arrival_seq"] for row in refunds), default=0) + 1
    refunds.append(
        {
            "event_no": str(payload.get("event_no") or f"{order.order_no}-RF{arrival_seq:02d}"),
            "kind": str(payload.get("kind") or "item"),
            "sku_code": str(payload.get("sku_code") or ""),
            "qty": int(payload.get("qty") or 0),
            "occurred_at": payload.get("occurred_at"),
            "amount_declared": payload.get("amount_declared"),
            "arrival_seq": arrival_seq,
            "note": payload.get("note"),
        }
    )
    items = _stored_item_inputs(db, order)
    discounts = _stored_discount_inputs(db, order)
    try:
        settle = _engine_payload(items, discounts, refunds, order.shipping_fee, order.payable_declared)
    except ValueError as exc:
        raise AppError("VALIDATION", 422, {"field": "refund", "reason": str(exc)}) from exc
    _guard_refund_replay(settle)
    _persist_settlement(db, order, settle)
    _persist_refunds(db, order, settle)
    db.flush()
    return order


def resettle(db: Session, order: Order) -> Order:
    """按当前落库的货品/优惠/退款事件重新结算（修复数据或回放后对齐）。"""
    settle = _engine_payload(
        _stored_item_inputs(db, order),
        _stored_discount_inputs(db, order),
        _stored_refund_inputs(db, order),
        order.shipping_fee,
        order.payable_declared,
    )
    _persist_settlement(db, order, settle)
    _persist_refunds(db, order, settle)
    db.flush()
    return order


def get_order(db: Session, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None:
        raise not_found("order", order_id)
    return order


def serialize(db: Session, order: Order, *, detail: bool = False) -> dict:
    competitor = db.get(Competitor, order.competitor_id)
    refunds = db.scalars(
        select(RefundEvent).where(RefundEvent.order_id == order.id).order_by(RefundEvent.business_seq, RefundEvent.id)
    ).all()
    settle = order.settle or {}
    data = {
        "id": order.id,
        "order_no": order.order_no,
        "competitor_id": order.competitor_id,
        "competitor_name": competitor.name if competitor else None,
        "competitor_code": competitor.code if competitor else None,
        "paid_on": _display_time(order.paid_on),
        "channel": order.channel,
        "campaign": order.campaign,
        "items_total": round(order.items_total, 2),
        "discount_total": round(order.discount_total, 2),
        "shipping_fee": round(order.shipping_fee, 2),
        "payable_declared": round(order.payable_declared, 2),
        "payable_after_refund": round(order.payable_after_refund, 2),
        "refunded_total": round(order.refunded_total, 2),
        "status": order.status,
        "status_label": ORDER_STATUS_LABELS.get(order.status, order.status),
        "reconcile_state": order.reconcile_state,
        "reconcile_label": RECONCILE_STATE_LABELS.get(order.reconcile_state, order.reconcile_state),
        "reconcile_checks": order.reconcile_checks or [],
        "refund_count": len(refunds),
        "late_refund_count": sum(1 for row in refunds if row.business_seq != row.arrival_seq),
        "legacy_gap": round(float((settle.get("totals") or {}).get("legacy_gap", 0.0)), 2),
        "item_count": len(settle.get("items") or []),
        "note": order.note,
        "created_at": iso_utc(order.created_at),
    }
    if detail:
        names = {
            row.sku_code: row.name for row in db.scalars(select(Sku)).all()
        }
        data["items"] = [
            {**item, "name": item.get("name") or names.get(item["sku_code"], "")}
            for item in (settle.get("items") or [])
        ]
        data["discounts"] = [
            {
                **discount,
                "kind_label": ORDER_DISCOUNT_KIND_LABELS.get(discount.get("kind"), discount.get("kind")),
            }
            for discount in settle.get("discounts") or []
        ]
        data["totals"] = settle.get("totals") or {}
        data["refund_events"] = [
            {
                "id": row.id,
                "event_no": row.event_no,
                "kind": row.kind,
                "kind_label": ORDER_REFUND_KIND_LABELS.get(row.kind, row.kind),
                "sku_code": row.sku_code,
                "qty": row.qty,
                "occurred_at": _display_time(row.occurred_at),
                "arrival_seq": row.arrival_seq,
                "business_seq": row.business_seq,
                "late_arrival": row.arrival_seq != row.business_seq,
                "amount_declared": row.amount_declared,
                "amount_reversed": round(row.amount_reversed, 2),
                "variance": round(row.variance, 2),
                "state": row.state,
                "state_label": REFUND_EVENT_STATE_LABELS.get(row.state, row.state),
                "error": row.error,
                "reversal": row.reversal,
                "note": row.note,
            }
            for row in refunds
        ]
    return data


def list_orders(
    db: Session,
    *,
    competitor_id: int | None = None,
    reconcile_state: str | None = None,
    q: str | None = None,
    paid_on: date | None = None,
    limit: int = 40,
    offset: int = 0,
) -> dict:
    stmt = select(Order)
    count_stmt = select(func.count()).select_from(Order)
    if competitor_id:
        stmt = stmt.where(Order.competitor_id == competitor_id)
        count_stmt = count_stmt.where(Order.competitor_id == competitor_id)
    if reconcile_state:
        stmt = stmt.where(Order.reconcile_state == reconcile_state)
        count_stmt = count_stmt.where(Order.reconcile_state == reconcile_state)
    if q:
        like = f"%{q.strip()}%"
        condition = or_(Order.order_no.ilike(like), Order.campaign.ilike(like))
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    if paid_on:
        start = datetime(paid_on.year, paid_on.month, paid_on.day) - timedelta(minutes=_offset())
        stmt = stmt.where(Order.paid_on >= start, Order.paid_on < start + timedelta(days=1))
        count_stmt = count_stmt.where(Order.paid_on >= start, Order.paid_on < start + timedelta(days=1))
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(Order.paid_on.desc(), Order.id.desc()).limit(limit).offset(offset)).all()
    return {
        "items": [serialize(db, row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def summary(db: Session) -> dict:
    total = db.scalar(select(func.count()).select_from(Order)) or 0
    passed = db.scalar(select(func.count()).select_from(Order).where(Order.reconcile_state == "passed")) or 0
    failed = total - passed
    refunded = db.scalar(select(func.count()).select_from(Order).where(Order.refunded_total > 0)) or 0
    late = db.scalar(
        select(func.count()).select_from(RefundEvent).where(RefundEvent.arrival_seq != RefundEvent.business_seq)
    ) or 0
    gaps = [row[0] or {} for row in db.execute(select(Order.settle)).all()]
    legacy_gap = 0.0
    for settle in gaps:
        legacy_gap += float(((settle or {}).get("totals") or {}).get("legacy_gap", 0.0))
    return {
        "orders_total": total,
        "reconcile_passed": passed,
        "reconcile_failed": failed,
        "refunded_orders": refunded,
        "late_refund_events": late,
        "legacy_gap_total": round(legacy_gap, 2),
    }


def comparable_samples(db: Session, sku: Sku, *, as_of: date, days: int = COMPARABLE_WINDOW_DAYS) -> list[dict]:
    """已对账通过订单中该货号的券后单价（回冲后剩余件数口径），作为跟价建议样本。

    只有 reconcile_state = passed 的订单才算可比依据；回冲后剩余件数为 0 的行不出样本。
    """
    rows = _sample_rows(db, as_of=as_of, days=days, competitor_id=sku.competitor_id, sku_code=sku.sku_code)
    samples: list[dict] = []
    for order, item in rows:
        samples.append(_sample_of(order, item))
    return samples


def _offset() -> int:
    return settings.app_timezone_offset_minutes


def _local_day_start(as_of: date, days: int) -> datetime:
    """业务时区的近 N 天窗口起点，换算成库里存储的 UTC naive。"""
    start_local = datetime(as_of.year, as_of.month, as_of.day) - timedelta(days=days - 1)
    return start_local - timedelta(minutes=_offset())


def _sample_rows(db: Session, *, as_of: date, days: int, competitor_id: int | None = None, sku_code: str | None = None):
    stmt = (
        select(Order, OrderItem)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(
            Order.reconcile_state == "passed",
            Order.paid_on >= _local_day_start(as_of, days),
            Order.paid_on < datetime(as_of.year, as_of.month, as_of.day) + timedelta(days=1) - timedelta(minutes=_offset()),
            OrderItem.remaining_qty > 0,
        )
        .order_by(Order.paid_on, Order.id)
    )
    if competitor_id is not None:
        stmt = stmt.where(Order.competitor_id == competitor_id)
    if sku_code is not None:
        stmt = stmt.where(OrderItem.sku_code == sku_code)
    return db.execute(stmt).all()


def _sample_of(order: Order, item: OrderItem) -> dict:
    return {
        "basis": SampleBasis.ORDER.value,
        "order_id": order.id,
        "order_no": order.order_no,
        "paid_on": (order.paid_on + timedelta(minutes=_offset())).date().isoformat(),
        "line_no": item.line_no,
        "qty": item.remaining_qty,
        "unit_price": round(float(item.remaining_net) / item.remaining_qty, 2) if item.remaining_qty else 0.0,
        "order_unit_price": round(float(item.effective_unit_price), 2),
        "discount_allocated": round(float(item.discount_allocated), 2),
        "refunded_qty": item.refunded_qty,
    }


def comparable_samples_bulk(db: Session, *, as_of: date, days: int = COMPARABLE_WINDOW_DAYS) -> dict[str, list[dict]]:
    """一次查出窗口内全部「对账通过」订单的货号样本，避免逐 SKU 查询。"""
    grouped: dict[str, list[dict]] = {}
    for order, item in _sample_rows(db, as_of=as_of, days=days):
        grouped.setdefault(item.sku_code, []).append(_sample_of(order, item))
    return grouped
