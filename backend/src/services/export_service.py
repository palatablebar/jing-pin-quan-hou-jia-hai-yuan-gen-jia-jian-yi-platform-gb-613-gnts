"""导出服务：跟价建议清单 / 券后价快照 CSV（两个算法的结果都进导出）。"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import (
    ADVICE_ACTION_LABELS,
    ADVICE_STATUS_LABELS,
    CATEGORY_LABELS,
    RISK_LABELS,
    RUN_STATUS_LABELS,
    TRIGGER_LABELS,
)
from src.config.settings import settings
from src.models.advice import Advice
from src.models.competitor import Competitor
from src.models.order import Order, OrderAllocation, OrderDiscount, OrderItem, RefundEvent
from src.models.run import Run
from src.models.sku import Sku
from src.models.snapshot import Snapshot
from src.utils.csv_export import to_csv


def _r2(value) -> str:
    return f"{float(value):.2f}"


def _local(dt) -> str:
    """库里存 UTC naive，导出按业务时区（默认 UTC+8）展示。"""
    if not dt:
        return ""
    return (dt + timedelta(minutes=settings.app_timezone_offset_minutes)).strftime("%Y-%m-%d %H:%M")


def _coupon_text(snapshot_fields: dict) -> str:
    parts: list[str] = []
    for tier in snapshot_fields.get("full_reduction") or []:
        parts.append(f"满{tier.get('threshold'):g}减{tier.get('minus'):g}")
    shop = snapshot_fields.get("shop_coupon") or {}
    if shop:
        parts.append(f"店铺券满{shop.get('threshold'):g}减{shop.get('minus'):g}")
    platform = snapshot_fields.get("platform_coupon") or {}
    if platform:
        parts.append(f"平台每满{platform.get('per'):g}减{platform.get('minus'):g}")
    discount = snapshot_fields.get("member_discount") or 1.0
    if float(discount) < 1.0:
        parts.append(f"会员{float(discount) * 100:g}%折")
    return "＋".join(parts)


def advices_csv(db: Session, run_id: int | None = None) -> bytes:
    stmt = select(Advice)
    if run_id:
        stmt = stmt.where(Advice.run_id == run_id)
    advices = db.scalars(stmt.order_by(Advice.severity, Advice.id)).all()
    skus = {row.id: row for row in db.scalars(select(Sku)).all()}
    competitors = {row.id: row for row in db.scalars(select(Competitor)).all()}
    runs = {row.id: row for row in db.scalars(select(Run)).all()}
    headers = [
        "建议编号", "运行号", "SKU编码", "SKU名称", "类目", "竞品店铺",
        "页面价", "券后1件", "券后2件", "券后3件", "命中组合",
        "竞品P10", "竞品P25", "竞品P50", "我方当前价", "建议价", "价差%", "建议毛利%",
        "风险", "动作", "状态", "规则", "审批人", "审批备注", "生成时间",
    ]
    rows = []
    for advice in advices:
        sku = skus.get(advice.sku_id)
        competitor = competitors.get(advice.competitor_id)
        run = runs.get(advice.run_id)
        snapshot = (advice.payload or {}).get("snapshot") or {}
        effective = {
            "full_reduction": snapshot.get("full_reduction"),
            "shop_coupon": snapshot.get("shop_coupon"),
            "platform_coupon": snapshot.get("platform_coupon"),
            "member_discount": snapshot.get("member_discount"),
        }
        rows.append(
            [
                advice.id,
                run.run_no if run else "",
                sku.sku_code if sku else "",
                sku.name if sku else "",
                CATEGORY_LABELS.get(sku.category, sku.category) if sku else "",
                competitor.name if competitor else "",
                f"{snapshot.get('page_price', ''):.2f}" if snapshot.get("page_price") else "",
                f"{snapshot.get('unit_price_1'):.2f}" if snapshot.get("unit_price_1") else "",
                f"{snapshot.get('unit_price_2'):.2f}" if snapshot.get("unit_price_2") else "",
                f"{snapshot.get('unit_price_3'):.2f}" if snapshot.get("unit_price_3") else "",
                snapshot.get("note") or _coupon_text(effective),
                f"{advice.p10:.2f}",
                f"{advice.p25:.2f}",
                f"{advice.p50:.2f}",
                f"{advice.current_price:.2f}",
                f"{advice.suggested_price:.2f}",
                f"{advice.gap_pct:.2f}",
                f"{advice.margin_pct:.2f}",
                RISK_LABELS.get(advice.severity, advice.severity),
                ADVICE_ACTION_LABELS.get(advice.action, advice.action),
                ADVICE_STATUS_LABELS.get(advice.status, advice.status),
                advice.rule_code,
                advice.approver or "",
                advice.decide_note or "",
                advice.created_at.strftime("%Y-%m-%d %H:%M") if advice.created_at else "",
            ]
        )
    return to_csv(headers, rows)


def snapshots_csv(db: Session, captured_on=None) -> bytes:
    stmt = select(Snapshot)
    if captured_on:
        stmt = stmt.where(Snapshot.captured_on == captured_on)
    snapshots = db.scalars(stmt.order_by(Snapshot.captured_on.desc(), Snapshot.id)).all()
    skus = {row.id: row for row in db.scalars(select(Sku)).all()}
    competitors = {row.id: row for row in db.scalars(select(Competitor)).all()}
    headers = [
        "快照编号", "快照日期", "SKU编码", "SKU名称", "类目", "竞品店铺",
        "页面价", "满减", "店铺券", "平台券", "会员折扣",
        "券后1件", "券后2件", "券后3件", "最优件数", "命中组合", "券后折扣%", "采集活动",
    ]
    rows = []
    for snapshot in snapshots:
        sku = skus.get(snapshot.sku_id)
        competitor = competitors.get(snapshot.competitor_id)
        effective = snapshot.effective or {}
        rows.append(
            [
                snapshot.id,
                snapshot.captured_on.isoformat() if snapshot.captured_on else "",
                sku.sku_code if sku else "",
                sku.name if sku else "",
                CATEGORY_LABELS.get(sku.category, sku.category) if sku else "",
                competitor.name if competitor else "",
                f"{snapshot.page_price:.2f}",
                _coupon_text({"full_reduction": snapshot.full_reduction}),
                _coupon_text({"shop_coupon": snapshot.shop_coupon}),
                _coupon_text({"platform_coupon": snapshot.platform_coupon}),
                f"{snapshot.member_discount:g}",
                _fmt(effective.get("unit_price_1")),
                _fmt(effective.get("unit_price_2")),
                _fmt(effective.get("unit_price_3")),
                effective.get("best_units") or "",
                effective.get("note") or "",
                _fmt(effective.get("discount_pct")),
                snapshot.campaign,
            ]
        )
    return to_csv(headers, rows)


def _fmt(value) -> str:
    if value is None or value == "":
        return ""
    return f"{float(value):.2f}"


def runs_csv(db: Session) -> bytes:
    runs = db.scalars(select(Run).order_by(Run.id)).all()
    headers = ["运行号", "快照日期", "触发方式", "状态", "输入快照", "建议条数", "高风险", "平均价差%", "操作人", "开始时间", "结束时间"]
    rows = [
        [
            run.run_no,
            run.captured_on.isoformat() if run.captured_on else "",
            TRIGGER_LABELS.get(run.trigger, run.trigger),
            RUN_STATUS_LABELS.get(run.status, run.status),
            run.input_count,
            run.output_count,
            (run.metrics or {}).get("high", 0),
            (run.metrics or {}).get("avg_gap_pct", 0),
            run.operator_email,
            run.started_at.strftime("%Y-%m-%d %H:%M") if run.started_at else "",
            run.finished_at.strftime("%Y-%m-%d %H:%M") if run.finished_at else "",
        ]
        for run in runs
    ]
    return to_csv(headers, rows)


def allocations_csv(db: Session, order_id: int | None = None) -> bytes:
    """订单整单优惠分摊明细：每笔优惠 × 每个货号一行，可直接对账。"""
    stmt = (
        select(Order, OrderItem, OrderDiscount, OrderAllocation)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(OrderAllocation, OrderAllocation.item_id == OrderItem.id)
        .join(OrderDiscount, OrderDiscount.id == OrderAllocation.discount_id)
        .order_by(Order.paid_on, Order.id, OrderItem.line_no, OrderDiscount.seq)
    )
    if order_id:
        stmt = stmt.where(Order.id == order_id)
    headers = [
        "订单号", "成交时间", "竞品店铺", "货号", "件数", "成交单价", "成交金额",
        "优惠顺序", "整单优惠类型", "整单优惠说明", "整单优惠金额",
        "分摊权重（成交金额）", "分摊到本行", "本行券后净额", "券后单价",
        "已回冲", "剩余分摊", "对账状态",
    ]
    rows = []
    for order, item, discount, alloc in db.execute(stmt).all():
        competitor = db.get(Competitor, order.competitor_id)
        rows.append(
            [
                order.order_no,
                _local(order.paid_on),
                competitor.name if competitor else "",
                item.sku_code,
                item.qty,
                _r2(item.unit_price),
                _r2(item.gross_amount),
                discount.seq,
                discount.kind,
                discount.label,
                _r2(discount.amount),
                _r2(alloc.weight),
                _r2(alloc.amount),
                _r2(item.net_amount),
                _r2(item.effective_unit_price),
                _r2(alloc.refunded_amount),
                _r2(alloc.remaining_amount),
                order.reconcile_state,
            ]
        )
    return to_csv(headers, rows)


def refunds_csv(db: Session, order_id: int | None = None) -> bytes:
    """回款变更流水：按业务发生时间排序，标出到达顺序与业务顺序的差异。"""
    stmt = select(Order, RefundEvent).join(RefundEvent, RefundEvent.order_id == Order.id)
    if order_id:
        stmt = stmt.where(Order.id == order_id)
    stmt = stmt.order_by(Order.paid_on, Order.id, RefundEvent.business_seq, RefundEvent.id)
    headers = [
        "订单号", "退款事件号", "类型", "货号", "件数", "业务发生时间",
        "到达序号", "业务顺序", "晚到", "回款流水实退", "按分摊口径回冲", "差额",
        "回放结果", "未回放原因", "对账状态",
    ]
    rows = []
    for order, event in db.execute(stmt).all():
        rows.append(
            [
                order.order_no,
                event.event_no,
                event.kind,
                event.sku_code,
                event.qty,
                _local(event.occurred_at),
                event.arrival_seq,
                event.business_seq,
                "是" if event.arrival_seq != event.business_seq else "否",
                "" if event.amount_declared is None else _r2(event.amount_declared),
                _r2(event.amount_reversed),
                _r2(event.variance),
                event.state,
                event.error or "",
                order.reconcile_state,
            ]
        )
    return to_csv(headers, rows)


def reconciliations_csv(db: Session) -> bytes:
    """整单对账报表：订单实付 vs 逐行分摊 vs 回冲，附旧口径偏差。"""
    headers = [
        "订单号", "成交时间", "竞品店铺", "状态", "商品成交额", "整单优惠合计",
        "运费", "订单实付", "已回冲", "回冲后应付", "剩余净额", "剩余件数",
        "旧口径逐行抵扣合计", "旧口径偏差", "对账结论",
    ]
    rows = []
    for order in db.scalars(select(Order).order_by(Order.paid_on, Order.id)).all():
        competitor = db.get(Competitor, order.competitor_id)
        totals = (order.settle or {}).get("totals") or {}
        rows.append(
            [
                order.order_no,
                _local(order.paid_on),
                competitor.name if competitor else "",
                order.status,
                _r2(order.items_total),
                _r2(order.discount_total),
                _r2(order.shipping_fee),
                _r2(order.payable_declared),
                _r2(order.refunded_total),
                _r2(order.payable_after_refund),
                _r2(totals.get("remaining_net", 0)),
                totals.get("remaining_qty", 0),
                _r2(totals.get("legacy_discount_total", 0)),
                _r2(totals.get("legacy_gap", 0)),
                order.reconcile_state,
            ]
        )
    return to_csv(headers, rows)
