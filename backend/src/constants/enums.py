"""共享枚举（后端唯一定义处）。前端副本见 frontend/site/js/config.js。

README「共享枚举出现位置」一节列出全部出现点，改动需两侧同步。
"""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"


class Platform(StrEnum):
    """竞品店铺所在平台（决定券的种类与叠加方式）。"""

    DOUYIN = "douyin"
    TAOBAO = "taobao"
    JD = "jd"
    PDD = "pdd"
    XHS = "xhs"
    WEIDIAN = "weidian"


PLATFORM_LABELS: dict[str, str] = {
    Platform.DOUYIN.value: "抖音小店",
    Platform.TAOBAO.value: "天猫 / 淘宝",
    Platform.JD.value: "京东",
    Platform.PDD.value: "拼多多",
    Platform.XHS.value: "小红书",
    Platform.WEIDIAN.value: "微店",
}


class Category(StrEnum):
    SOFA = "sofa"
    TABLE = "table"
    BED = "bed"
    CABINET = "cabinet"
    DECOR = "decor"


CATEGORY_LABELS: dict[str, str] = {
    Category.SOFA.value: "沙发",
    Category.TABLE.value: "桌几",
    Category.BED.value: "床具",
    Category.CABINET.value: "柜类",
    Category.DECOR.value: "软装",
}


class RuleStrategy(StrEnum):
    """跟价策略：决定建议价锚点如何被规则改写。"""

    GUARDED = "guarded"  # 有底线跟价：跟到 P25×1.02，但不破成本/底价
    AGGRESSIVE = "aggressive"  # 抢量：目标毛利压低，跟随更紧
    HOLD = "hold"  # 只守不降：维持当前价，仅记录竞品差


RULE_STRATEGY_LABELS: dict[str, str] = {
    RuleStrategy.GUARDED.value: "有底线跟价",
    RuleStrategy.AGGRESSIVE.value: "抢量紧跟",
    RuleStrategy.HOLD.value: "只守不降",
}


class RiskLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


RISK_LABELS: dict[str, str] = {
    RiskLevel.HIGH.value: "高风险",
    RiskLevel.MEDIUM.value: "中风险",
    RiskLevel.LOW.value: "低风险",
}

RISK_ORDER: list[str] = [RiskLevel.HIGH.value, RiskLevel.MEDIUM.value, RiskLevel.LOW.value]


class AdviceStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


ADVICE_STATUS_LABELS: dict[str, str] = {
    AdviceStatus.PENDING.value: "待审批",
    AdviceStatus.APPROVED.value: "已批改价",
    AdviceStatus.REJECTED.value: "已驳回",
}

ADVICE_STATUS_ORDER: list[str] = [
    AdviceStatus.PENDING.value,
    AdviceStatus.APPROVED.value,
    AdviceStatus.REJECTED.value,
]


class AdviceAction(StrEnum):
    """建议动作（算法 B 输出，页面与导出共用）。"""

    FOLLOW = "follow"
    HOLD = "hold"
    RAISE = "raise"


ADVICE_ACTION_LABELS: dict[str, str] = {
    AdviceAction.FOLLOW.value: "跟价下调",
    AdviceAction.HOLD.value: "维持不动",
    AdviceAction.RAISE.value: "回调上行",
}


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


RUN_STATUS_LABELS: dict[str, str] = {
    RunStatus.QUEUED.value: "排队中",
    RunStatus.RUNNING.value: "执行中",
    RunStatus.SUCCESS.value: "成功",
    RunStatus.PARTIAL.value: "部分成功",
    RunStatus.FAILED.value: "失败",
}


class TriggerKind(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    SEED = "seed"


TRIGGER_LABELS: dict[str, str] = {
    TriggerKind.MANUAL.value: "手动",
    TriggerKind.SCHEDULE.value: "定时",
    TriggerKind.SEED.value: "初始化",
}


class CouponKind(StrEnum):
    """券后价还原涉及的折扣类型。"""

    FULL_REDUCTION = "full_reduction"  # 满减阶梯（单档命中）
    SHOP_COUPON = "shop_coupon"  # 店铺券（可与满减叠加）
    PLATFORM_COUPON = "platform_coupon"  # 平台跨店券（每满 X 减 Y，可重复）
    MEMBER_DISCOUNT = "member_discount"  # 会员折扣率


COUPON_KIND_LABELS: dict[str, str] = {
    CouponKind.FULL_REDUCTION.value: "满减",
    CouponKind.SHOP_COUPON.value: "店铺券",
    CouponKind.PLATFORM_COUPON.value: "平台券",
    CouponKind.MEMBER_DISCOUNT.value: "会员折扣",
}


class OrderDiscountKind(StrEnum):
    """订单整单优惠类型（按业务顺序逐笔分摊到货号）。"""

    PLATFORM_CROSS_STORE = "platform_cross_store"  # 平台跨店券：整单门槛只发一张，绝不能逐行全额抵
    SHOP_COUPON = "shop_coupon"  # 店铺券（整单）
    FULL_REDUCTION = "full_reduction"  # 满减（整单）
    MEMBER = "member"  # 会员立减/折扣（整单）
    OTHER = "other"


ORDER_DISCOUNT_KIND_LABELS: dict[str, str] = {
    OrderDiscountKind.PLATFORM_CROSS_STORE.value: "平台跨店券",
    OrderDiscountKind.SHOP_COUPON.value: "店铺券",
    OrderDiscountKind.FULL_REDUCTION.value: "满减",
    OrderDiscountKind.MEMBER.value: "会员优惠",
    OrderDiscountKind.OTHER.value: "其他整单优惠",
}


class OrderStatus(StrEnum):
    """订单履约/回款状态（由回冲后的剩余件数推导）。"""

    PAID = "paid"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"


ORDER_STATUS_LABELS: dict[str, str] = {
    OrderStatus.PAID.value: "已成交",
    OrderStatus.PARTIALLY_REFUNDED.value: "部分退款",
    OrderStatus.REFUNDED.value: "已全额退款",
}


class ReconcileState(StrEnum):
    """整单分摊与订单实付对账结论；只有 passed 的订单才能作为可比依据。"""

    PASSED = "passed"
    FAILED = "failed"


RECONCILE_STATE_LABELS: dict[str, str] = {
    ReconcileState.PASSED.value: "对账通过",
    ReconcileState.FAILED.value: "对账不通过",
}


class OrderRefundKind(StrEnum):
    """回款变更（退款事件）类型。"""

    ITEM = "item"  # 按货号退件
    ORDER = "order"  # 整单剩余全退
    SHIPPING = "shipping"  # 只退运费


ORDER_REFUND_KIND_LABELS: dict[str, str] = {
    OrderRefundKind.ITEM.value: "按货号退件",
    OrderRefundKind.ORDER.value: "整单全退",
    OrderRefundKind.SHIPPING.value: "退运费",
}


class RefundEventState(StrEnum):
    """退款事件回放结果。"""

    APPLIED = "applied"
    INVALID = "invalid"


REFUND_EVENT_STATE_LABELS: dict[str, str] = {
    RefundEventState.APPLIED.value: "已回冲",
    RefundEventState.INVALID.value: "未回冲",
}


class SampleBasis(StrEnum):
    """跟价建议的可比价格来源。"""

    ORDER = "order"  # 已对账订单按货号成交金额分摊整单优惠后的券后单价
    SNAPSHOT = "snapshot"  # 回退到 RPA 抓价快照的券后单件价


SAMPLE_BASIS_LABELS: dict[str, str] = {
    SampleBasis.ORDER.value: "订单分摊券后价",
    SampleBasis.SNAPSHOT.value: "抓价快照券后价",
}
