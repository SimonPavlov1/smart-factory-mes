from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.time_utils import utcnow
from app.models.auth import User
from app.models.production import Order, OrderCancellationObligation
from app.services.auth_service import get_current_user, require_roles, user_has_role
from app.services.cancellation_service import (
    analyze_order_cancellation,
    approve_order_cancellation,
    resolve_obligation,
    try_finalize_cancellation,
)


router = APIRouter(prefix="/manufacturing/orders", tags=["Отмена заказов"])


class CancellationRequestPayload(BaseModel):
    reason: str = Field(min_length=3)


class CancellationResolutionPayload(BaseModel):
    decision: str
    note: Optional[str] = None
    quantity_returned: Optional[float] = None
    quantity_used: Optional[float] = None
    quantity_damaged: Optional[float] = None
    financial_impact: Optional[float] = None
    details: dict = Field(default_factory=dict)


def _order_or_404(db: Session, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return order


def _obligation_payload(item: OrderCancellationObligation):
    return {
        "id": item.id,
        "order_id": item.order_id,
        "obligation_type": item.obligation_type,
        "status": item.status,
        "responsible_role": item.responsible_role,
        "task_id": item.task_id,
        "description": item.description,
        "resolution": item.resolution,
        "created_at": item.created_at,
        "resolved_at": item.resolved_at,
        "resolved_by_user_id": item.resolved_by_user_id,
    }


@router.post("/{order_id}/cancellation/request")
def request_cancellation(
    order_id: int,
    payload: CancellationRequestPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "manager", "production_manager")),
):
    order = _order_or_404(db, order_id)
    if order.cancellation_status in {"settlement", "cancelled", "cancelled_with_commitments"}:
        raise HTTPException(status_code=409, detail="Отмена заказа уже обрабатывается или завершена")
    order.cancellation_status = "cancellation_requested"
    order.cancellation_reason = payload.reason.strip()
    order.cancellation_requested_at = utcnow()
    order.cancellation_requested_by = user.id
    order.status = "Cancellation Requested"
    obligations = analyze_order_cancellation(db, order, user.id)
    if user_has_role(user, "production_manager"):
        approve_order_cancellation(db, order, user)
    db.commit()
    return {
        "order_id": order.id,
        "cancellation_status": order.cancellation_status,
        "obligations": [_obligation_payload(item) for item in obligations],
    }


@router.post("/{order_id}/cancellation/approve")
def approve_cancellation(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "production_manager")),
):
    order = _order_or_404(db, order_id)
    approve_order_cancellation(db, order, user)
    finalized = try_finalize_cancellation(db, order, user.id)
    db.commit()
    return {"order_id": order.id, "cancellation_status": order.cancellation_status, "finalized": finalized}


@router.get("/{order_id}/cancellation")
def get_cancellation(
    order_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    order = _order_or_404(db, order_id)
    obligations = db.query(OrderCancellationObligation).filter_by(order_id=order.id).order_by(OrderCancellationObligation.id).all()
    return {
        "order_id": order.id,
        "cancellation_status": order.cancellation_status,
        "reason": order.cancellation_reason,
        "requested_at": order.cancellation_requested_at,
        "requested_by": order.cancellation_requested_by,
        "approved_at": order.cancellation_approved_at,
        "approved_by": order.cancellation_approved_by,
        "cancelled_at": order.cancelled_at,
        "financial_impact": order.financial_impact,
        "summary": order.cancellation_summary,
        "obligations": [_obligation_payload(item) for item in obligations],
    }


@router.post("/{order_id}/cancellation/obligations/{obligation_id}/resolve")
def resolve_cancellation_obligation(
    order_id: int,
    obligation_id: int,
    payload: CancellationResolutionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    order = _order_or_404(db, order_id)
    obligation = db.query(OrderCancellationObligation).filter_by(id=obligation_id, order_id=order.id).first()
    if not obligation:
        raise HTTPException(status_code=404, detail="Обязательство не найдено")
    if not user_has_role(user, obligation.responsible_role, "manager", "production_manager"):
        raise HTTPException(status_code=403, detail="Обязательство назначено другому отделу")
    resolution = payload.dict(exclude_none=True)
    if payload.financial_impact:
        order.financial_impact = float(order.financial_impact or 0) + float(payload.financial_impact)
    resolve_obligation(db, obligation, user, resolution)
    finalized = try_finalize_cancellation(db, order, user.id)
    db.commit()
    return {
        "obligation": _obligation_payload(obligation),
        "cancellation_status": order.cancellation_status,
        "finalized": finalized,
    }
