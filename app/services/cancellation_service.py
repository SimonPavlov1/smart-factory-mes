from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.inventory import Stock
from app.models.production import (
    MaterialTransfer,
    Order,
    OrderCancellationObligation,
    Reservation,
    WorkflowTask,
)
from app.services.task_management_service import calculate_sla_due, record_event
from app.time_utils import utcnow


ACTIVE_TASK_STATUSES = {"open", "assigned", "in_progress", "hold", "waiting_delivery", "ready_to_issue"}
CANCELLATION_TASK_PREFIX = "cancellation_"


def _produced_qty(task: WorkflowTask) -> float:
    payload = task.payload or {}
    assignments = payload.get("assembly_assignments") or []
    assignment_qty = sum(float(item.get("produced_qty") or 0) for item in assignments)
    daily_qty = sum(float(item.get("qty") or 0) for item in payload.get("daily_progress") or [])
    return max(assignment_qty, daily_qty)


def _add_obligation(
    db: Session,
    order: Order,
    obligation_type: str,
    responsible_role: str,
    description: str,
    actor_user_id: int,
) -> OrderCancellationObligation:
    obligation = OrderCancellationObligation(
        order_id=order.id,
        obligation_type=obligation_type,
        responsible_role=responsible_role,
        description=description,
    )
    db.add(obligation)
    db.flush()
    task = WorkflowTask(
        order_id=order.id,
        type=f"{CANCELLATION_TASK_PREFIX}{obligation_type}",
        title=description,
        description="Закрыть обязательство отмены и зафиксировать принятое решение.",
        role=responsible_role,
        status="open",
        created_by_user_id=actor_user_id,
        is_manual=False,
        priority="high",
        sla_due_at=calculate_sla_due("high"),
        payload={"cancellation_obligation_id": obligation.id},
    )
    db.add(task)
    db.flush()
    obligation.task_id = task.id
    record_event(
        db, task, "created", actor_user_id, to_status="open",
        data={"cancellation_obligation_id": obligation.id, "order_id": order.id},
    )
    return obligation


def analyze_order_cancellation(db: Session, order: Order, actor_user_id: int) -> list[OrderCancellationObligation]:
    existing = db.query(OrderCancellationObligation).filter_by(order_id=order.id).all()
    if existing:
        return existing

    obligations = []
    reservations = db.query(Reservation).filter(Reservation.order_id == order.id, Reservation.qty > 0).all()
    if reservations:
        obligations.append(_add_obligation(
            db, order, "release_reservations", "warehouse",
            f"Снять резерв материалов по заказу #{order.id}",
            actor_user_id,
        ))

    active_tasks = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order.id,
        WorkflowTask.status.in_(ACTIVE_TASK_STATUSES),
    ).all()
    procurement = [task for task in active_tasks if task.type == "procurement_purchase"]
    if procurement:
        obligations.append(_add_obligation(
            db, order, "procurement_commitments", "procurement",
            f"Урегулировать активные закупки по заказу #{order.id}",
            actor_user_id,
        ))

    accounting = [task for task in active_tasks if task.type == "accounting_payment"]
    if accounting:
        obligations.append(_add_obligation(
            db, order, "financial_commitments", "accounting",
            f"Зафиксировать финансовые последствия отмены заказа #{order.id}",
            actor_user_id,
        ))

    transfers = db.query(MaterialTransfer).filter(
        MaterialTransfer.order_id == order.id,
        MaterialTransfer.status.in_(["issued", "accepted"]),
    ).all()
    if transfers:
        obligations.append(_add_obligation(
            db, order, "issued_materials", "warehouse",
            f"Сверить и вернуть неиспользованные материалы заказа #{order.id}",
            actor_user_id,
        ))

    assembly_tasks = [task for task in active_tasks if task.type == "assembler_build"]
    if any(_produced_qty(task) > 0 for task in assembly_tasks):
        obligations.append(_add_obligation(
            db, order, "wip_disposition", "production_manager",
            f"Принять решение по незавершённому производству заказа #{order.id}",
            actor_user_id,
        ))

    if not obligations:
        obligations.append(_add_obligation(
            db, order, "stop_confirmation", "production_manager",
            f"Подтвердить остановку заказа #{order.id}",
            actor_user_id,
        ))
    return obligations


def approve_order_cancellation(db: Session, order: Order, actor: User):
    if order.cancellation_status not in {"cancellation_requested", "stopping"}:
        raise HTTPException(status_code=409, detail="Отмена заказа не запрошена")
    order.cancellation_status = "settlement"
    order.cancellation_approved_at = utcnow()
    order.cancellation_approved_by = actor.id
    order.status = "Cancellation Settlement"


def resolve_obligation(
    db: Session,
    obligation: OrderCancellationObligation,
    actor: User,
    resolution: dict,
):
    if obligation.status == "resolved":
        return
    if not resolution.get("decision"):
        raise HTTPException(status_code=422, detail="Укажите принятое решение")

    if obligation.obligation_type == "release_reservations":
        reservations = db.query(Reservation).filter(Reservation.order_id == obligation.order_id).all()
        for reservation in reservations:
            stock = db.query(Stock).filter(Stock.component_id == reservation.component_id).with_for_update().first()
            if stock:
                stock.reserved_qty = max(
                    float(stock.reserved_qty or 0) - float(reservation.qty or 0),
                    0,
                )
            db.delete(reservation)

    obligation.status = "resolved"
    obligation.resolution = resolution
    obligation.resolved_at = utcnow()
    obligation.resolved_by_user_id = actor.id
    if obligation.task_id:
        task = db.get(WorkflowTask, obligation.task_id)
        if task and task.status != "done":
            previous = task.status
            task.status = "done"
            task.completed_at = utcnow()
            task.payload = {**(task.payload or {}), "resolution": resolution}
            record_event(
                db, task, "completed", actor.id,
                from_status=previous, to_status="done",
                data={"resolution": resolution},
            )


def try_finalize_cancellation(db: Session, order: Order, actor_user_id: int | None = None) -> bool:
    if order.cancellation_status != "settlement":
        return False
    obligations = db.query(OrderCancellationObligation).filter_by(order_id=order.id).all()
    if not obligations or any(item.status != "resolved" for item in obligations):
        return False

    active_tasks = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order.id,
        WorkflowTask.status.in_(ACTIVE_TASK_STATUSES),
        ~WorkflowTask.type.startswith(CANCELLATION_TASK_PREFIX),
    ).all()
    for task in active_tasks:
        previous = task.status
        task.status = "cancelled"
        task.cancelled_at = utcnow()
        task.cancel_reason = order.cancellation_reason
        record_event(
            db, task, "cancelled_by_order", actor_user_id,
            from_status=previous, to_status="cancelled",
            reason=order.cancellation_reason,
        )

    resolutions = [item.resolution or {} for item in obligations]
    has_commitments = any(
        resolution.get("decision") not in {"released", "cancelled", "stopped", "returned"}
        for resolution in resolutions
    )
    order.cancellation_status = "cancelled_with_commitments" if has_commitments else "cancelled"
    order.status = "Cancelled"
    order.cancelled_at = utcnow()
    order.cancellation_summary = {
        "obligations": len(obligations),
        "has_commitments": has_commitments,
    }
    return True
