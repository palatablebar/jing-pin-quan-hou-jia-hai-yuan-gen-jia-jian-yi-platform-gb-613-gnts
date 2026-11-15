"""订单整单优惠分摊 + 回款回冲 + 对账闸门单测（纯函数）。"""
from __future__ import annotations

import pytest

from src.services.order_allocation import settle_order, split_by_weight

ITEMS = [
    # 三件套：沙发 2 件 600，茶几 1 件 300，落地灯 1 件 100
    {"line_no": 1, "sku_code": "PB-sofa-001", "qty": 2, "unit_price": 600.0},
    {"line_no": 2, "sku_code": "PB-table-002", "qty": 1, "unit_price": 300.0},
    {"line_no": 3, "sku_code": "PB-decor-003", "qty": 1, "unit_price": 100.0},
]

# 跨店券「每满 1200 减 150」只发一张，整单减 150；店铺券满 800 减 50
DISCOUNTS = [
    {"seq": 1, "kind": "shop_coupon", "label": "店铺券满800减50", "amount": 50.0},
    {"seq": 2, "kind": "platform_cross_store", "label": "跨店每满1200减150", "amount": 150.0},
]


def test_order_level_discount_is_allocated_by_gross_amount():
    result = settle_order(items=ITEMS, discounts=DISCOUNTS, shipping_fee=0)
    totals = result["totals"]
    assert totals["items_total"] == 1600.0
    assert totals["discount_total"] == 200.0
    assert totals["payable"] == 1400.0
    # 按成交金额 1200 : 300 : 100 分摊 200 → 150 / 37.5 / 12.5
    by_line = {row["line_no"]: row for row in result["items"]}
    assert by_line[1]["discount_allocated"] == 150.0
    assert by_line[2]["discount_allocated"] == 37.5
    assert by_line[3]["discount_allocated"] == 12.5
    assert sum(row["discount_allocated"] for row in result["items"]) == 200.0
    # 券后单价：沙发 525，茶几 262.5，落地灯 87.5
    assert by_line[1]["effective_unit_price"] == 525.0
    assert by_line[2]["effective_unit_price"] == 262.5
    assert by_line[3]["effective_unit_price"] == 87.5
    assert result["reconcile_state"] == "passed"


def test_legacy_row_full_deduction_is_flagged_as_gap():
    result = settle_order(items=ITEMS, discounts=DISCOUNTS)
    # 旧口径：每个货号都被整单 200 全额抵扣，逐行加总 600 << 实付 1400
    # 旧口径逐行加总：500×2 + 100 + 0 = 1100，比实付 1400 少 300
    assert result["totals"]["legacy_gap"] == pytest.approx(-300.0)
    assert result["totals"]["legacy_discount_total"] == 600.0
    assert result["items"][0]["legacy_unit_price"] == 500.0  # 600×2−200 再摊 2 件
    assert result["items"][1]["legacy_unit_price"] == 100.0


def test_allocation_rounding_never_loses_a_cent():
    items = [
        {"line_no": 1, "sku_code": "A", "qty": 1, "unit_price": 33.33},
        {"line_no": 2, "sku_code": "B", "qty": 1, "unit_price": 33.33},
        {"line_no": 3, "sku_code": "C", "qty": 1, "unit_price": 33.34},
    ]
    result = settle_order(items=items, discounts=[{"kind": "platform_cross_store", "amount": 10.0}])
    assert sum(row["discount_allocated"] for row in result["items"]) == 10.0
    assert result["reconcile_state"] == "passed"


def test_split_by_weight_is_stable_and_exact():
    assert split_by_weight(100, [1, 1, 1], [1, 2, 3]) == [34, 33, 33]
    assert split_by_weight(0, [5, 5], [1, 2]) == [0, 0]
    with pytest.raises(ValueError):
        split_by_weight(10, [0, 0], [1, 2])


def test_payable_mismatch_blocks_comparability():
    result = settle_order(items=ITEMS, discounts=DISCOUNTS, payable_declared=1350.0)
    assert result["reconcile_state"] == "failed"
    failed = [row for row in result["checks"] if not row["passed"]]
    assert any(row["key"] == "payable_declared" for row in failed)
    # 分摊本身仍然自洽，问题只在实付对不上
    assert result["totals"]["legacy_gap"] == pytest.approx(-250.0)


def test_partial_refund_reverses_net_and_allocation_proportionally():
    result = settle_order(
        items=ITEMS,
        discounts=DISCOUNTS,
        refunds=[
            {"event_no": "R1", "kind": "item", "sku_code": "PB-sofa-001", "qty": 1, "occurred_at": "2026-11-10T09:00:00"},
        ],
    )
    sofa = result["items"][0]
    assert sofa["refunded_qty"] == 1
    assert sofa["refunded_amount"] == 525.0  # 单件净额 525
    assert sofa["remaining_qty"] == 1
    assert sofa["remaining_net"] == 525.0
    assert sofa["remaining_unit_price"] == 525.0
    assert result["totals"]["refunded_total"] == 525.0
    assert result["totals"]["payable_after_refund"] == 875.0
    assert result["status"] == "partially_refunded"
    assert result["reconcile_state"] == "passed"
    # 回冲也按分摊拆：沙发两笔优惠各退一半（50/2 + 150/2 = 75）
    refund = result["refunds"][0]
    assert refund["amount_reversed"] == 525.0
    assert sum(row["amount"] for row in refund["reversal"]["allocations"]) == 75.0  # 沙发两笔优惠各退一半


def test_late_refund_event_is_replayed_in_business_order():
    """先到的是「后发生」的整单退款；按到达顺序处理会失败或金额错，按业务时间才对。"""
    result = settle_order(
        items=ITEMS,
        discounts=DISCOUNTS,
        refunds=[
            # 到达序号 1，但业务发生时间更晚：整单剩余全退
            {"event_no": "LATE-ORDER", "kind": "order", "occurred_at": "2026-11-12T18:00:00", "arrival_seq": 1},
            # 到达序号 2，但业务发生时间更早：先退 1 件沙发
            {
                "event_no": "EARLY-ITEM",
                "kind": "item",
                "sku_code": "PB-sofa-001",
                "qty": 1,
                "occurred_at": "2026-11-11T10:00:00",
                "arrival_seq": 2,
            },
        ],
    )
    events = {row["event_no"]: row for row in result["refunds"]}
    assert events["EARLY-ITEM"]["business_seq"] == 1
    assert events["LATE-ORDER"]["business_seq"] == 2
    assert events["EARLY-ITEM"]["late_arrival"] is True
    assert events["LATE-ORDER"]["late_arrival"] is True
    assert events["EARLY-ITEM"]["amount_reversed"] == 525.0
    # 整单退款退掉剩余 3 件（875）
    assert events["LATE-ORDER"]["amount_reversed"] == 875.0
    assert result["totals"]["refunded_total"] == 1400.0
    assert result["status"] == "refunded"
    assert result["totals"]["payable_after_refund"] == 0.0
    assert result["reconcile_state"] == "passed"


def test_refund_with_declared_amount_mismatch_fails_reconcile():
    result = settle_order(
        items=ITEMS,
        discounts=DISCOUNTS,
        refunds=[
            {
                "event_no": "R1",
                "kind": "item",
                "sku_code": "PB-table-002",
                "qty": 1,
                "occurred_at": "2026-11-11T10:00:00",
                "amount_declared": 300.0,  # 平台退的是页面价，没扣分摊
            }
        ],
    )
    assert result["refunds"][0]["amount_reversed"] == 262.5
    assert result["refunds"][0]["variance"] == 37.5
    assert result["reconcile_state"] == "failed"
    assert any(row["key"] == "refund_reversal" and not row["passed"] for row in result["checks"])


def test_refund_beyond_remaining_qty_is_invalid_and_not_applied():
    result = settle_order(
        items=ITEMS,
        discounts=DISCOUNTS,
        refunds=[
            {"event_no": "R1", "kind": "item", "sku_code": "PB-sofa-001", "qty": 3, "occurred_at": "2026-11-11T10:00:00"},
        ],
    )
    assert result["refunds"][0]["state"] == "invalid"
    assert result["refunds"][0]["amount_reversed"] == 0.0
    assert result["totals"]["refunded_total"] == 0.0
    assert result["reconcile_state"] == "failed"


def test_shipping_refund_is_separate_from_item_net():
    result = settle_order(
        items=ITEMS,
        discounts=DISCOUNTS,
        shipping_fee=12.0,
        refunds=[{"event_no": "S1", "kind": "shipping", "occurred_at": "2026-11-11T09:00:00", "amount_declared": 12.0}],
    )
    assert result["totals"]["payable"] == 1412.0
    assert result["refunds"][0]["amount_reversed"] == 12.0
    assert result["totals"]["payable_after_refund"] == 1400.0
    assert result["reconcile_state"] == "passed"


def test_discounts_cannot_exceed_items_total():
    with pytest.raises(ValueError):
        settle_order(items=ITEMS[:1], discounts=[{"amount": 2000.0}])


def test_settlement_is_deterministic():
    first = settle_order(items=ITEMS, discounts=DISCOUNTS)
    second = settle_order(items=ITEMS, discounts=DISCOUNTS)
    assert first == second
