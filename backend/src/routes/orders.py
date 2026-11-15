from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import ORDER_READ_ROLES, ORDER_WRITE_ROLES
from src.controllers import order_controller
from src.middlewares.rbac import require_roles
from src.schemas.order import OrderIn, RefundCreateIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("")
def list_orders(
    competitor_id: int | None = None,
    reconcile_state: str | None = Query(default=None, pattern=r"^(passed|failed)$"),
    q: str | None = None,
    paid_on: date | None = None,
    limit: int = Query(40),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*ORDER_READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return order_controller.list_orders(
        db,
        competitor_id=competitor_id,
        reconcile_state=reconcile_state,
        q=q,
        paid_on=paid_on,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{order_id}")
def get_order(
    order_id: int, db: Session = Depends(get_db), principal: dict = Depends(require_roles(*ORDER_READ_ROLES))
) -> dict:
    return order_controller.get_order(db, order_id)


@router.post("", status_code=201)
def create_order(
    payload: OrderIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*ORDER_WRITE_ROLES)),
) -> dict:
    return order_controller.create_order(db, payload.model_dump(), principal, request)


@router.post("/{order_id}/refunds", status_code=201)
def create_refund(
    order_id: int,
    payload: RefundCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*ORDER_WRITE_ROLES)),
) -> dict:
    return order_controller.create_refund(db, order_id, payload.model_dump(), principal, request)


@router.post("/{order_id}/resettle")
def resettle(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*ORDER_WRITE_ROLES)),
) -> dict:
    return order_controller.resettle(db, order_id, principal, request)
