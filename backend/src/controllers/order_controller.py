"""订单分摊控制器：列表 / 明细 / 录入 / 追加回款变更。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, order_service


def list_orders(db: Session, **filters) -> dict:
    data = order_service.list_orders(db, **filters)
    data["summary"] = order_service.summary(db)
    return data


def get_order(db: Session, order_id: int) -> dict:
    return order_service.serialize(db, order_service.get_order(db, order_id), detail=True)


def create_order(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    order = order_service.create_order(db, payload)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="order.capture",
        entity="order",
        entity_id=order.id,
        detail={
            "order_no": order.order_no,
            "payable": order.payable_declared,
            "reconcile_state": order.reconcile_state,
            "refunds": len((order.settle or {}).get("refunds") or []),
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return order_service.serialize(db, order, detail=True)


def create_refund(db: Session, order_id: int, payload: dict, principal: dict, request: Request) -> dict:
    order = order_service.get_order(db, order_id)
    order_service.add_refund(db, order, payload)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="order.refund",
        entity="order",
        entity_id=order.id,
        detail={
            "order_no": order.order_no,
            "kind": payload.get("kind"),
            "sku_code": payload.get("sku_code"),
            "qty": payload.get("qty"),
            "amount_reversed": order.refunded_total,
            "reconcile_state": order.reconcile_state,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return order_service.serialize(db, order, detail=True)


def resettle(db: Session, order_id: int, principal: dict, request: Request) -> dict:
    order = order_service.get_order(db, order_id)
    order_service.resettle(db, order)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="order.resettle",
        entity="order",
        entity_id=order.id,
        detail={"order_no": order.order_no, "reconcile_state": order.reconcile_state},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return order_service.serialize(db, order, detail=True)
