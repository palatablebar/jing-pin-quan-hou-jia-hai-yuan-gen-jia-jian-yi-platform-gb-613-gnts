"""种子数据与规则匹配的确定性校验（不连数据库，纯内存计算）。"""
from __future__ import annotations

from collections import Counter

from scripts.demo_config import (
    CAPTURE_DAYS,
    FOLLOW_COUNT,
    HIGH_COUNT,
    MEDIUM_COUNT,
    SKU_COUNT,
    SHOWPIECE_CODE,
    build_dataset,
)
from src.services import effective_price, follow_advice
from src.services.rule_service import match_rule
from types import SimpleNamespace


def _rule_objects(dataset):
    rules = []
    for index, spec in enumerate(sorted(dataset["rules"], key=lambda item: item["priority"])):
        rules.append(
            SimpleNamespace(
                id=index + 1,
                code=spec["code"],
                scope_kind=spec["scope_kind"],
                scope_value=spec["scope_value"],
                sku_id=None,
                strategy=spec["strategy"],
                target_margin_pct=spec["target_margin_pct"],
                max_drop_pct=spec["max_drop_pct"],
                priority=spec["priority"],
            )
        )
    return rules


def _sku_object(spec):
    return SimpleNamespace(id=spec["index"] + 1, sku_code=spec["sku_code"], category=spec["category"])


def test_dataset_shape():
    dataset = build_dataset()
    assert len(dataset["competitors"]) == 6
    assert len(dataset["skus"]) == SKU_COUNT
    assert len(dataset["snapshots"]) == SKU_COUNT * CAPTURE_DAYS
    assert len(dataset["rules"]) == 5


def test_dataset_produces_expected_advice_counts():
    dataset = build_dataset()
    prices: dict[str, list[float]] = {}
    for snapshot in dataset["snapshots"]:
        result = effective_price.compute_effective_price(snapshot["spec"])
        assert result["valid"] is True, snapshot["sku_code"]
        prices.setdefault(snapshot["sku_code"], []).append(result["unit_price_1"])
    rules = _rule_objects(dataset)
    skus = {spec["sku_code"]: spec for spec in dataset["skus"]}

    risks: Counter[str] = Counter()
    advices = 0
    for spec in dataset["skus"]:
        rule = match_rule(rules, _sku_object(spec), spec["competitor_code"])
        result = follow_advice.build_advice(
            competitor_prices=prices[spec["sku_code"]],
            cost=spec["cost"],
            floor_price=spec["floor_price"],
            target_margin_pct=rule.target_margin_pct,
            current_price=spec["current_price"],
            strategy=rule.strategy,
            max_drop_pct=rule.max_drop_pct,
        )
        if not result["need_follow"]:
            continue
        advices += 1
        risks[result["risk"]] += 1
        assert result["suggested_price"] >= spec["floor_price"]  # 不破底价

    assert advices == FOLLOW_COUNT
    assert risks["high"] == HIGH_COUNT
    assert risks["medium"] == MEDIUM_COUNT
    assert risks["low"] == FOLLOW_COUNT - HIGH_COUNT - MEDIUM_COUNT
    assert skus[SHOWPIECE_CODE]["current_price"] == 319.0


def test_showpiece_numbers_are_stable():
    dataset = build_dataset()
    snapshots = [item for item in dataset["snapshots"] if item["sku_code"] == SHOWPIECE_CODE]
    assert len(snapshots) == CAPTURE_DAYS
    effective = effective_price.compute_effective_price(snapshots[0]["spec"])
    assert (effective["unit_price_1"], effective["unit_price_2"], effective["unit_price_3"]) == (279.0, 244.0, 245.67)


def test_rule_scope_priority_order():
    demo = build_dataset()
    rules = _rule_objects(demo)
    sofa_showpiece = next(item for item in demo["skus"] if item["sku_code"] == SHOWPIECE_CODE)
    # SKU 级规则优先于竞品规则（该 SKU 对标 dy-muyan，抖音规则优先级 40）
    assert match_rule(rules, _sku_object(sofa_showpiece), "dy-muyan").code == "rule-sku-showpiece"
    decor = next(item for item in demo["skus"] if item["category"] == "decor")
    assert match_rule(rules, _sku_object(decor), decor["competitor_code"]).code == "rule-decor-hold"
    other = next(item for item in demo["skus"] if item["category"] == "bed" and item["competitor_code"] == "wd-nanmu")
    assert match_rule(rules, _sku_object(other), "wd-nanmu").code == "rule-global"


def test_order_samples_settle_and_gate_comparability():
    """订单样例：整单分摊对账通过、晚到退款按业务时间回放、失败单被闸门拦住。"""
    from src.services.order_allocation import settle_order

    dataset = build_dataset()
    orders = dataset["orders"]
    assert len(orders) == 5
    settled = {}
    for order in orders:
        settled[order["order_no"]] = settle_order(
            shipping_fee=order.get("shipping_fee") or 0,
            payable_declared=order.get("payable_declared"),
            items=[{**item, "line_no": index + 1} for index, item in enumerate(order["items"])],
            discounts=order["discounts"],
            refunds=[
                {**refund, "occurred_at": f"2026-11-15T{refund.get('occurred_hour', 10):02d}:00:00"}
                for refund in order.get("refunds") or []
            ],
        )
    allocation_orders = [settled[order["order_no"]] for order in orders[:3]]
    for result in allocation_orders:
        assert result["reconcile_state"] == "passed"
        assert result["totals"]["discount_total"] == 60.0
        assert result["totals"]["legacy_gap"] == -60.0
        assert {row["sku_code"]: row["discount_allocated"] for row in result["items"]} == {
            "PB-sofa-001": 40.0,
            "PB-decor-115": 20.0,
        }

    late = settled["SO-2618-004"]
    assert late["reconcile_state"] == "passed"
    assert [row["event_no"] for row in late["refunds"]] == ["RF-004-EARLY", "RF-004-LATE"]
    assert late["totals"]["refunded_total"] == 2378.0
    assert late["status"] == "refunded"

    failed = settled["SO-2618-005"]
    assert failed["reconcile_state"] == "failed"
    assert "payable_declared" in {row["key"] for row in failed["checks"] if not row["passed"]}
