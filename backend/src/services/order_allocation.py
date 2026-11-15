"""订单整单优惠分摊与回款回冲（纯函数、确定性、只依赖标准库）。

背景（旧口径的错误）：平台跨店券按「整单门槛」发放，一张券只减一次；
旧实现把同一张券分别全额抵扣到每个货号，导致：

    Σ 逐行券后价 << 订单实付，且货号越多偏差越大 → 跟价建议被系统性压低。

本模块的结算口径：

    1. 每笔整单优惠只记一次金额（order_discounts），不复制到行；
    2. 按各货号成交金额（单价 × 数量）比例分摊整单优惠，分值用「最大余数法」，
       保证 Σ逐行分摊 == 整单优惠合计（精确到分，不丢不重）；
    3. 行券后净额 = 成交金额 − 分摊额；券后单价 = 净额 / 数量；
    4. 退款事件（回款变更）按业务发生时间回放（晚到事件也按发生顺序回冲），
       回冲金额按该行剩余数量比例拆到各笔优惠分摊上，保持「净额 + 分摊 = 成交额」；
    5. 对账：整单优惠分摊合计、逐行净额合计、订单实付、退款回冲四组数对平才算
       reconcile_state = passed，只有通过才能作为跟价建议的可比依据。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("金额不能是布尔值")
    return Decimal(str(value if value is not None else 0))


def _cents(value) -> int:
    return int((_dec(value) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _money(cents: int) -> float:
    return float((Decimal(cents) / 100).quantize(CENT, rounding=ROUND_HALF_UP))


def _unit(cents: int, qty: int) -> int:
    """净额 ÷ 件数，四舍五入到分（整数分）。"""
    if qty <= 0:
        return 0
    return int((Decimal(cents) / qty).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def split_by_weight(total_cents: int, weights: list[int], keys: list) -> list[int]:
    """按权重把总额（分）拆成整数份额，最大余数法；Σ份额 == total_cents。

    keys 只用于余数相同时的稳定排序，保证同样输入两次结果一致。
    """
    if total_cents < 0:
        raise ValueError("分摊金额不能为负")
    total_weight = sum(weights)
    if total_weight <= 0:
        if total_cents == 0:
            return [0] * len(weights)
        raise ValueError("分摊权重合计为 0，无法分摊")
    shares: list[int] = []
    remainders: list[tuple[Decimal, Decimal, str, int]] = []
    for index, weight in enumerate(weights):
        if weight <= 0:
            shares.append(0)
            continue
        exact = Decimal(total_cents) * weight / total_weight
        base = int(exact)
        shares.append(base)
        remainders.append((exact - base, Decimal(weight), str(keys[index]), index))
    leftover = total_cents - sum(shares)
    remainders.sort(key=lambda row: (-row[0], -row[1], row[2]))
    for _, _, _, index in remainders[:leftover]:
        shares[index] += 1
    return shares


def split_discounts_by_weight(discount_amounts: list[int], weights: list[int], keys: list) -> list[list[int]]:
    """把多笔优惠依次按权重分摊，每笔都用「累计份额」求差，避免逐笔取整漂移。

    单笔最大余数法每笔都可能多给某行 1 分；优惠笔数一多，小额货号的累计分摊
    就会系统性偏多。这里对「累计优惠额」做最大余数法，本笔份额 = 累计目标 − 已分，
    保证每笔份额合计精确、且每个货号的累计分摊始终紧贴精确比例。
    """
    count = len(weights)
    total_weight = sum(weights)
    if total_weight <= 0:
        if not any(discount_amounts):
            return [[0] * count for _ in discount_amounts]
        raise ValueError("分摊权重合计为 0，无法分摊")
    allocated = [0] * count
    cumulative = 0
    results: list[list[int]] = []
    for amount in discount_amounts:
        if amount < 0:
            raise ValueError("分摊金额不能为负")
        cumulative += amount
        targets: list[int] = []
        remainders: list[tuple[Decimal, Decimal, str, int]] = []
        for index, weight in enumerate(weights):
            if weight <= 0:
                targets.append(0)
                continue
            exact = Decimal(cumulative) * weight / total_weight
            base = int(exact)
            targets.append(base)
            remainders.append((exact - base, Decimal(weight), str(keys[index]), index))
        leftover = cumulative - sum(targets)
        remainders.sort(key=lambda row: (-row[0], -row[1], row[2]))
        for _, _, _, index in remainders[:leftover]:
            targets[index] += 1
        shares = [target - allocated[index] for index, target in enumerate(targets)]
        # 极端情况下某行累计目标回落 1 分：把这 1 分从最大的份额挪过去，保持总量
        for index, share in enumerate(shares):
            while share < 0:
                donor = max(range(count), key=lambda i: (shares[i], -i))
                if donor == index or shares[donor] <= 0:
                    break
                shares[donor] -= 1
                shares[index] += 1
        allocated = [allocated[index] + share for index, share in enumerate(shares)]
        results.append(shares)
    return results


def _parse_time(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value or "").strip()
    if not text:
        raise ValueError("退款事件缺少发生时间")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"退款事件发生时间格式非法：{text}") from exc
    return parsed.replace(tzinfo=None)


def _clean_items(raw_items: list[dict]) -> list[dict]:
    if not raw_items:
        raise ValueError("订单至少需要一个货号")
    items: list[dict] = []
    seen_lines: set[int] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError("订单货品必须是对象")
        line_no = int(raw.get("line_no") or index + 1)
        if line_no in seen_lines:
            raise ValueError(f"行号重复：{line_no}")
        seen_lines.add(line_no)
        qty = int(raw.get("qty") or 0)
        if qty <= 0:
            raise ValueError(f"货号 {raw.get('sku_code') or line_no} 的件数必须大于 0")
        unit_price = _dec(raw.get("unit_price"))
        if unit_price < 0:
            raise ValueError("成交单价不能为负")
        if unit_price > Decimal(1000000):
            raise ValueError("成交单价超出合理区间")
        sku_code = str(raw.get("sku_code") or "").strip()
        if not sku_code:
            raise ValueError("货号编码不能为空")
        items.append(
            {
                "line_no": line_no,
                "sku_id": raw.get("sku_id"),
                "sku_code": sku_code,
                "name": str(raw.get("name") or ""),
                "qty": qty,
                "unit_price_cents": _cents(unit_price),
                "gross_cents": _cents(unit_price) * qty,
            }
        )
    items.sort(key=lambda row: row["line_no"])
    return items


def _clean_discounts(raw_discounts: list[dict], items_total_cents: int) -> list[dict]:
    discounts: list[dict] = []
    seen_seq: set[int] = set()
    for index, raw in enumerate(raw_discounts or []):
        if not isinstance(raw, dict):
            raise ValueError("整单优惠必须是对象")
        seq = int(raw.get("seq") or index + 1)
        if seq in seen_seq:
            raise ValueError(f"优惠顺序重复：{seq}")
        seen_seq.add(seq)
        amount = _dec(raw.get("amount"))
        if amount < 0:
            raise ValueError("整单优惠金额不能为负")
        if amount > Decimal(1000000):
            raise ValueError("整单优惠金额超出合理区间")
        discounts.append(
            {
                "seq": seq,
                "kind": str(raw.get("kind") or "other"),
                "label": str(raw.get("label") or ""),
                "amount_cents": _cents(amount),
                "rule": raw.get("rule") or {},
                "allocations": [],
            }
        )
    discounts.sort(key=lambda row: row["seq"])
    total = sum(row["amount_cents"] for row in discounts)
    if total > items_total_cents:
        raise ValueError("整单优惠合计不能超过商品成交金额")
    return discounts


def _allocate(discounts: list[dict], items: list[dict]) -> None:
    """逐笔把整单优惠按成交金额分摊到行；每条分摊记录同时挂到优惠和行两侧。"""
    weights = [item["gross_cents"] for item in items]
    keys = [item["line_no"] for item in items]
    per_discount = split_discounts_by_weight([discount["amount_cents"] for discount in discounts], weights, keys)
    for discount, shares in zip(discounts, per_discount):
        discount["allocations"] = [
            {
                "discount_seq": discount["seq"],
                "line_no": item["line_no"],
                "weight_cents": item["gross_cents"],
                "allocated_cents": share,
                "refunded_cents": 0,
                "remaining_cents": share,
            }
            for item, share in zip(items, shares)
        ]


def _apply_refund(lines_by_code: dict[str, list[dict]], event: dict, totals: dict) -> dict:
    """把一条退款事件回冲到货品行与其分摊明细；返回回冲明细或抛 ValueError。"""
    kind = event["kind"]
    reversal = {"lines": [], "allocations": [], "shipping_cents": 0}

    if kind == "shipping":
        if event["qty"]:
            raise ValueError("退运费事件不应带件数")
        if totals["shipping_remaining_cents"] <= 0:
            raise ValueError("该订单没有可退的运费")
        amount = totals["shipping_remaining_cents"]
        totals["shipping_remaining_cents"] = 0
        reversal["shipping_cents"] = amount
        return reversal

    if kind == "order":
        targets = [
            (line, line["remaining_qty"])
            for item in lines_by_code.values()
            for line in item
            if line["remaining_qty"] > 0
        ]
        if not targets:
            raise ValueError("订单已无剩余件数，无法整单退款")
    elif kind == "item":
        sku_code = event.get("sku_code")
        if not sku_code or sku_code not in lines_by_code:
            raise ValueError(f"退款货号不在订单货品组成内：{sku_code}")
        qty = int(event.get("qty") or 0)
        if qty <= 0:
            raise ValueError("退件数量必须大于 0")
        candidates = lines_by_code[sku_code]
        remaining = sum(line["remaining_qty"] for line in candidates)
        if qty > remaining:
            raise ValueError(f"退件数量 {qty} 超过该货号剩余件数 {remaining}")
        targets = []
        left = qty
        for line in candidates:
            if left <= 0:
                break
            take = min(left, line["remaining_qty"])
            if take > 0:
                targets.append((line, take))
                left -= take
    else:
        raise ValueError(f"未知退款类型：{kind}")

    for line, qty in targets:
        before_net = line["remaining_net_cents"]
        before_gross = line["remaining_gross_cents"]
        keep_qty = line["remaining_qty"] - qty
        # 分摊与净额都按「剩余数量」比例回冲；全退时一次退清，避免分币残差
        if keep_qty == 0:
            net_after = 0
            gross_after = 0
        else:
            net_after = int(
                (Decimal(before_net) * keep_qty / line["remaining_qty"]).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            )
            gross_after = int(
                (Decimal(before_gross) * keep_qty / line["remaining_qty"]).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            )
        net_reversed = before_net - net_after

        # 让 Σ各笔优惠剩余分摊 = 成交额剩余 − 净额，保持恒等式
        alloc_target = gross_after - net_after
        current_allocs = [alloc for alloc in line["allocations"] if alloc["remaining_cents"] > 0]
        current_total = sum(alloc["remaining_cents"] for alloc in current_allocs)
        if alloc_target < 0 or alloc_target > current_total:
            raise ValueError("回冲后分摊额超出剩余分摊，数据不一致")
        shares = split_by_weight(
            alloc_target,
            [alloc["remaining_cents"] for alloc in current_allocs],
            [alloc["discount_seq"] for alloc in current_allocs],
        )
        for alloc, target in zip(current_allocs, shares):
            reversed_cents = alloc["remaining_cents"] - target
            if reversed_cents:
                alloc["refunded_cents"] += reversed_cents
                alloc["remaining_cents"] = target
                line["refunded_alloc_cents"] += reversed_cents
                reversal["allocations"].append(
                    {"line_no": line["line_no"], "discount_seq": alloc["discount_seq"], "amount_cents": reversed_cents}
                )
        line["refunded_qty"] += qty
        line["refunded_net_cents"] += net_reversed
        line["remaining_qty"] = keep_qty
        line["remaining_net_cents"] = net_after
        line["remaining_gross_cents"] = gross_after
        reversal["lines"].append(
            {
                "line_no": line["line_no"],
                "sku_code": line["sku_code"],
                "qty": qty,
                "net_cents": net_reversed,
            }
        )
    return reversal


def settle_order(
    *,
    shipping_fee: float = 0.0,
    payable_declared: float | None = None,
    items: list[dict],
    discounts: list[dict] | None = None,
    refunds: list[dict] | None = None,
) -> dict:
    """结算一张订单：整单优惠分摊 → 退款按业务时间回放 → 对账。

    入参 items / discounts / refunds 均为普通 dict（便于直接来自接口或 seed）；
    返回的每个 cents 字段都会同时给出对应的元字段供展示。
    """
    clean_items = _clean_items(items)
    items_total_cents = sum(item["gross_cents"] for item in clean_items)
    clean_discounts = _clean_discounts(discounts or [], items_total_cents)
    discount_total_cents = sum(row["amount_cents"] for row in clean_discounts)
    shipping_cents = _cents(shipping_fee)
    if shipping_cents < 0:
        raise ValueError("运费不能为负")
    payable_cents = items_total_cents - discount_total_cents + shipping_cents
    payable_cents_declared = payable_cents if payable_declared is None else _cents(payable_declared)
    if payable_cents_declared < 0:
        raise ValueError("订单实付不能为负")

    _allocate(clean_discounts, clean_items)

    lines: list[dict] = []
    for item in clean_items:
        line_allocs = [
            alloc for discount in clean_discounts for alloc in discount["allocations"] if alloc["line_no"] == item["line_no"]
        ]
        allocated = sum(alloc["allocated_cents"] for alloc in line_allocs)
        net = item["gross_cents"] - allocated
        if net < 0:
            raise ValueError(f"货号 {item['sku_code']} 分摊后净额为负")
        lines.append(
            {
                **item,
                "allocations": line_allocs,
                "allocated_cents": allocated,
                "net_cents": net,
                "refunded_qty": 0,
                "refunded_net_cents": 0,
                "refunded_alloc_cents": 0,
                "remaining_qty": item["qty"],
                "remaining_net_cents": net,
                "remaining_gross_cents": item["gross_cents"],
            }
        )

    lines_by_code: dict[str, list[dict]] = {}
    for line in lines:
        lines_by_code.setdefault(line["sku_code"], []).append(line)

    totals = {"shipping_remaining_cents": shipping_cents}
    events: list[dict] = []
    for index, raw in enumerate(refunds or []):
        if not isinstance(raw, dict):
            raise ValueError("退款事件必须是对象")
        events.append(
            {
                "event_no": str(raw.get("event_no") or f"RF-{index + 1}"),
                "kind": str(raw.get("kind") or "item"),
                "sku_code": str(raw.get("sku_code") or ""),
                "qty": int(raw.get("qty") or 0),
                "occurred_at": _parse_time(raw.get("occurred_at")),
                "amount_declared": None if raw.get("amount_declared") is None else _dec(raw.get("amount_declared")),
                "arrival_seq": int(raw.get("arrival_seq") or index + 1),
                "note": raw.get("note"),
            }
        )
    # 关键：按业务发生时间回放，而不是按到达/入库顺序；同一时刻再按到达序号稳定排序
    events.sort(key=lambda row: (row["occurred_at"], row["arrival_seq"]))

    replayed: list[dict] = []
    for business_seq, event in enumerate(events, start=1):
        record = {
            "event_no": event["event_no"],
            "kind": event["kind"],
            "sku_code": event["sku_code"],
            "qty": event["qty"],
            "occurred_at": event["occurred_at"],
            "arrival_seq": event["arrival_seq"],
            "business_seq": business_seq,
            "late_arrival": event["arrival_seq"] != business_seq,
            "amount_declared": None if event["amount_declared"] is None else float(event["amount_declared"]),
            "note": event["note"],
            "state": "applied",
            "error": None,
            "amount_reversed_cents": 0,
            "variance_cents": 0,
            "reversal": {"lines": [], "allocations": [], "shipping_cents": 0},
        }
        try:
            reversal = _apply_refund(lines_by_code, event, totals)
        except ValueError as exc:
            record["state"] = "invalid"
            record["error"] = str(exc)
            replayed.append(record)
            continue
        reversed_cents = sum(row["net_cents"] for row in reversal["lines"]) + reversal["shipping_cents"]
        record["reversal"] = reversal
        record["amount_reversed_cents"] = reversed_cents
        if event["amount_declared"] is not None:
            record["variance_cents"] = _cents(event["amount_declared"]) - reversed_cents
        # 记录回放后「事件到达序号 → 业务序号」的映射，供页面解释晚到
        replayed.append(record)

    allocated_total_cents = sum(alloc["allocated_cents"] for discount in clean_discounts for alloc in discount["allocations"])
    net_total_cents = sum(line["net_cents"] for line in lines)
    remaining_net_cents = sum(line["remaining_net_cents"] for line in lines)
    remaining_qty = sum(line["remaining_qty"] for line in lines)
    refunded_total_cents = sum(row["amount_reversed_cents"] for row in replayed)
    payable_after_cents = payable_cents - refunded_total_cents
    declared_refunds_cents = sum(
        _cents(row["amount_declared"]) for row in replayed if row["amount_declared"] is not None
    )
    reversed_declared_cents = sum(
        row["amount_reversed_cents"] for row in replayed if row["amount_declared"] is not None
    )

    # ---- 对账 ----
    checks: list[dict] = []

    def add_check(key: str, label: str, expected: int, actual: int, hint: str = "") -> None:
        delta = actual - expected
        checks.append(
            {
                "key": key,
                "label": label,
                "expected": _money(expected),
                "actual": _money(actual),
                "delta": _money(delta),
                "passed": delta == 0,
                "hint": hint,
            }
        )

    add_check("allocation_total", "逐行分摊合计 = 整单优惠", discount_total_cents, allocated_total_cents, "Σ各货号分摊必须等于整单优惠，不能逐行全额抵扣")
    add_check("net_total", "Σ逐行券后净额 = 商品成交额 − 整单优惠", items_total_cents - discount_total_cents, net_total_cents)
    add_check("payable_declared", "订单实付（平台回传）= 成交额 − 优惠 + 运费", payable_cents, payable_cents_declared, "逐行券后价之和应与实付同源")
    add_check("refund_reversal", "退款回冲合计 = 回款流水实退合计", declared_refunds_cents, reversed_declared_cents, "按业务时间回放后，回冲金额与平台流水对齐")
    add_check(
        "refund_balance",
        "回冲后剩余净额 + 剩余运费 = 实付（申报值）− 已回冲",
        payable_cents_declared - refunded_total_cents,
        remaining_net_cents + totals["shipping_remaining_cents"],
        "剩余件数口径与剩余净额必须等于实付减去已回冲",
    )
    invalid_events = [row for row in replayed if row["state"] == "invalid"]
    if invalid_events:
        checks.append(
            {
                "key": "refund_events_valid",
                "label": "退款事件全部可回放",
                "expected": len(replayed),
                "actual": len(replayed) - len(invalid_events),
                "delta": -len(invalid_events),
                "passed": False,
                "hint": "；".join(f"{row['event_no']}：{row['error']}" for row in invalid_events),
            }
        )
    passed = all(row["passed"] for row in checks)

    if remaining_qty == 0:
        status = "refunded"
    elif remaining_qty < sum(line["qty"] for line in lines):
        status = "partially_refunded"
    else:
        status = "paid"

    # 旧口径对照：同一张整单券被分别全额抵到每个货号（这正是问题现象）
    legacy_rows = []
    for line in lines:
        legacy_net = max(0, line["gross_cents"] - discount_total_cents)
        legacy_rows.append(
            {
                "line_no": line["line_no"],
                "legacy_discount_cents": discount_total_cents,
                "legacy_net_cents": legacy_net,
                "legacy_unit_price_cents": _unit(legacy_net, line["qty"]),
            }
        )
    legacy_discount_total = sum(row["legacy_discount_cents"] for row in legacy_rows)
    legacy_payable = sum(row["legacy_net_cents"] for row in legacy_rows) + shipping_cents

    return {
        "items": [
            {
                "line_no": line["line_no"],
                "sku_id": line["sku_id"],
                "sku_code": line["sku_code"],
                "name": line["name"],
                "qty": line["qty"],
                "unit_price": _money(line["unit_price_cents"]),
                "gross_amount": _money(line["gross_cents"]),
                "discount_allocated": _money(line["allocated_cents"]),
                "net_amount": _money(line["net_cents"]),
                "effective_unit_price": _money(_unit(line["net_cents"], line["qty"])),
                "refunded_qty": line["refunded_qty"],
                "refunded_amount": _money(line["refunded_net_cents"]),
                "remaining_qty": line["remaining_qty"],
                "remaining_net": _money(line["remaining_net_cents"]),
                "remaining_unit_price": _money(_unit(line["remaining_net_cents"], line["remaining_qty"])),
                "allocations": [
                    {
                        "seq": alloc["discount_seq"],
                        "allocated": _money(alloc["allocated_cents"]),
                        "refunded": _money(alloc["refunded_cents"]),
                        "remaining": _money(alloc["remaining_cents"]),
                    }
                    for alloc in line["allocations"]
                ],
                "legacy_discount": _money(legacy_rows[lines.index(line)]["legacy_discount_cents"]),
                "legacy_net": _money(legacy_rows[lines.index(line)]["legacy_net_cents"]),
                "legacy_unit_price": _money(legacy_rows[lines.index(line)]["legacy_unit_price_cents"]),
            }
            for line in lines
        ],
        "discounts": [
            {
                "seq": discount["seq"],
                "kind": discount["kind"],
                "label": discount["label"],
                "amount": _money(discount["amount_cents"]),
                "rule": discount["rule"],
                "allocations": [
                    {
                        "line_no": alloc["line_no"],
                        "weight": _money(alloc["weight_cents"]),
                        "amount": _money(alloc["allocated_cents"]),
                        "refunded": _money(alloc["refunded_cents"]),
                        "remaining": _money(alloc["remaining_cents"]),
                    }
                    for alloc in discount["allocations"]
                ],
            }
            for discount in clean_discounts
        ],
        "refunds": [
            {
                "event_no": row["event_no"],
                "kind": row["kind"],
                "sku_code": row["sku_code"],
                "qty": row["qty"],
                "occurred_at": row["occurred_at"].isoformat(timespec="seconds"),
                "arrival_seq": row["arrival_seq"],
                "business_seq": row["business_seq"],
                "late_arrival": row["late_arrival"],
                "amount_declared": None if row["amount_declared"] is None else float(row["amount_declared"]),
                "amount_reversed": _money(row["amount_reversed_cents"]),
                "variance": _money(row["variance_cents"]),
                "state": row["state"],
                "error": row["error"],
                "note": row["note"],
                "reversal": {
                    "lines": [
                        {
                            "line_no": item["line_no"],
                            "sku_code": item["sku_code"],
                            "qty": item["qty"],
                            "amount": _money(item["net_cents"]),
                        }
                        for item in row["reversal"]["lines"]
                    ],
                    "allocations": [
                        {
                            "line_no": item["line_no"],
                            "seq": item["discount_seq"],
                            "amount": _money(item["amount_cents"]),
                        }
                        for item in row["reversal"]["allocations"]
                    ],
                    "shipping": _money(row["reversal"]["shipping_cents"]),
                },
            }
            for row in replayed
        ],
        "totals": {
            "items_total": _money(items_total_cents),
            "discount_total": _money(discount_total_cents),
            "shipping_fee": _money(shipping_cents),
            "payable": _money(payable_cents),
            "payable_declared": _money(payable_cents_declared),
            "refunded_total": _money(refunded_total_cents),
            "payable_after_refund": _money(payable_after_cents),
            "remaining_net": _money(remaining_net_cents),
            "remaining_qty": remaining_qty,
            "legacy_discount_total": _money(legacy_discount_total),
            "legacy_payable": _money(legacy_payable),
            "legacy_gap": _money(legacy_payable - payable_cents_declared),
        },
        "checks": checks,
        "reconcile_state": "passed" if passed else "failed",
        "status": status,
    }
