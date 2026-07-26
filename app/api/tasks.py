import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import hashlib
import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.time_utils import utcnow
from app.models.auth import User
from app.models.production import (
    MaterialTransfer,
    Item,
    Order,
    ProductType,
    TaskDependency,
    TaskEvent,
    TaskNotification,
    TaskWatcher,
    WorkflowCommand,
    WorkflowTask,
)
from app.services.auth_service import get_current_user, require_roles, user_has_role, user_roles
from app.services.workflow_service import _open_material_flow_tasks, add_procurement_purchase, cleanup_premature_assembly_tasks, complete_task, enrich_component_lines, ensure_assembly_tasks_after_receipts, ensure_missing_order_item_workflows, ensure_procurement_payment_tasks, merge_order_procurement_tasks, normalize_assembly_task_payload, normalize_task_daily_progress, reconcile_procurement_tasks_with_stock, reconcile_stock_reservations, split_aggregate_assembly_tasks
from app.services.xlsx_service import build_table_xlsx
from app.services.workflow_batch_service import ACCUMULATIVE_TYPES, batch_summary, pending_product_lines
from app.services.factory_number_service import create_product_units
from app.services.task_management_service import (
    DEPENDENCY_TYPES,
    FINAL_STATUSES,
    TASK_PRIORITIES,
    add_mentioned_watchers,
    add_watcher,
    calculate_sla_due,
    ensure_dependency_is_valid,
    ensure_not_blocked,
    record_event,
    task_runtime_payload,
    transition_task,
)

router = APIRouter(prefix="/tasks", tags=["Workflow задачи"])
UPLOAD_ROOT = Path("uploads/tasks")


class TaskCompletePayload(BaseModel):
    payload: Optional[dict] = None
    idempotency_key: Optional[str] = None


class TaskNotePayload(BaseModel):
    note: str


class TaskAssignPayload(BaseModel):
    user_id: Optional[int] = None


class TaskDeadlinePayload(BaseModel):
    due_date: Optional[str] = None
    reason: Optional[str] = None


class TaskTransitionPayload(BaseModel):
    status: str
    reason: Optional[str] = None


class ManualTaskCreatePayload(BaseModel):
    title: str
    description: Optional[str] = None
    role: str
    assigned_user_id: Optional[int] = None
    order_id: Optional[int] = None
    product_id: Optional[int] = None
    priority: str = "normal"
    planned_start_at: Optional[str] = None
    due_date: Optional[str] = None
    estimated_minutes: Optional[int] = None
    watcher_ids: list[int] = Field(default_factory=list)
    dependency_ids: list[int] = Field(default_factory=list)


class TaskUpdatePayload(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    role: Optional[str] = None
    priority: Optional[str] = None
    planned_start_at: Optional[str] = None
    due_date: Optional[str] = None
    estimated_minutes: Optional[int] = None
    actual_minutes: Optional[int] = None
    reason: Optional[str] = None


class TaskDependencyPayload(BaseModel):
    depends_on_task_id: int
    dependency_type: str = "blocks"


class TaskWatcherPayload(BaseModel):
    user_id: int


class TaskBulkPayload(BaseModel):
    task_ids: list[int]
    action: str
    user_id: Optional[int] = None
    due_date: Optional[str] = None
    priority: Optional[str] = None
    reason: Optional[str] = None


class AssemblyAllocationPayload(BaseModel):
    user_id: int
    quantity: float
    order_item_id: Optional[int] = None
    product_id: Optional[int] = None
    due_date: Optional[str] = None


class AssemblyPlanPayload(BaseModel):
    allocations: list[AssemblyAllocationPayload]


class TestingClaimsPayload(BaseModel):
    serial_numbers: list[str]
    action: str = "claim"


class TaskReorderPayload(BaseModel):
    column: Optional[str] = None
    ordered_ids: list[int]


class ProcurementPurchasePayload(BaseModel):
    component_id: int
    qty: float
    expected_date: Optional[str] = None
    invoice: Optional[str] = None
    supplier: Optional[str] = None
    comment: Optional[str] = None


def _user_payload(user: User | None):
    if not user:
        return None
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "roles": user_roles(user),
    }


def _task_payload(task: WorkflowTask, db: Session | None = None):
    payload = normalize_assembly_task_payload(db, task) if db and task.type == "assembler_build" else normalize_task_daily_progress(task)
    assigned_user = db.get(User, task.assigned_user_id) if db and task.assigned_user_id else None
    if db:
        payload = {**payload}
        if task.type in ACCUMULATIVE_TYPES:
            payload["batch_summary"] = batch_summary(db, task)
            payload["pending_product_lines"] = pending_product_lines(
                db,
                task,
                aggregate_all=task.type in ["tester_check", "packer_pack"],
            )
        unit_status_by_task_type = {
            "tester_check": "testing",
            "repair_defects": "repair",
            "packer_pack": "passed",
            "warehouse_finished_goods": "packed",
        }
        if task.type == "assembler_build" and payload.get("unit_ids"):
            assembly_units = (
                db.query(Item)
                .filter(Item.id.in_(payload["unit_ids"]))
                .order_by(Item.id.asc())
                .all()
            )
            payload["serial_number_statuses"] = {
                unit.serial_number: unit.status for unit in assembly_units
            }
            available_assembly_serials = {
                unit.serial_number for unit in assembly_units
                if unit.status in ["planned", "in_assembly"]
            }
            stored_claims = (task.payload or {}).get("assembly_claims")
            if stored_claims is None and task.assigned_user_id:
                stored_claims = {
                    serial_number: task.assigned_user_id
                    for serial_number in available_assembly_serials
                }
            assembly_claims = {
                serial_number: int(user_id)
                for serial_number, user_id in (stored_claims or {}).items()
                if serial_number in available_assembly_serials
            }
            claim_users = {
                user_id: db.get(User, user_id)
                for user_id in set(assembly_claims.values())
            }
            payload["assembly_claims"] = assembly_claims
            payload["assembly_claim_details"] = [
                {
                    "serial_number": serial_number,
                    "user_id": user_id,
                    "user_name": (
                        claim_users[user_id].full_name
                        or claim_users[user_id].username
                        if claim_users.get(user_id)
                        else f"Сотрудник #{user_id}"
                    ),
                }
                for serial_number, user_id in assembly_claims.items()
            ]
        if task.type in unit_status_by_task_type and payload.get("unit_ids"):
            task_units = (
                db.query(Item)
                .filter(Item.id.in_(payload["unit_ids"]))
                .order_by(Item.id.asc())
                .all()
            )
            pending_units = (
                unit for unit in task_units
                if unit.status == unit_status_by_task_type[task.type]
            )
            pending_units = list(pending_units)
            payload["pending_unit_ids"] = [unit.id for unit in pending_units]
            payload["pending_serial_numbers"] = [unit.serial_number for unit in pending_units]
            payload["serial_number_statuses"] = {
                unit.serial_number: unit.status for unit in task_units
            }
            products = {
                product_id: db.get(ProductType, product_id)
                for product_id in {unit.product_id for unit in task_units if unit.product_id}
            }
            payload["serial_units"] = [
                {
                    "id": unit.id,
                    "serial_number": unit.serial_number,
                    "status": unit.status,
                    "product_id": unit.product_id,
                    "product_name": products.get(unit.product_id).name if products.get(unit.product_id) else None,
                    "drawing_number": products.get(unit.product_id).drawing_number if products.get(unit.product_id) else None,
                }
                for unit in task_units
            ]
            if task.type == "tester_check":
                testing_claims = {
                    serial_number: int(user_id)
                    for serial_number, user_id in ((task.payload or {}).get("testing_claims") or {}).items()
                    if serial_number in {unit.serial_number for unit in pending_units}
                }
                claim_users = {
                    user_id: db.get(User, user_id)
                    for user_id in set(testing_claims.values())
                }
                payload["testing_claims"] = testing_claims
                payload["testing_claim_details"] = [
                    {
                        "serial_number": serial_number,
                        "user_id": user_id,
                        "user_name": (
                            claim_users[user_id].full_name
                            or claim_users[user_id].username
                            if claim_users.get(user_id)
                            else f"Сотрудник #{user_id}"
                        ),
                    }
                    for serial_number, user_id in testing_claims.items()
                ]
        if payload.get("shortages"):
            payload["shortages"] = enrich_component_lines(db, payload["shortages"])
        if payload.get("materials"):
            payload["materials"] = enrich_component_lines(db, payload["materials"])
        if payload.get("purchases"):
            payload["purchases"] = enrich_component_lines(db, payload["purchases"])
        if payload.get("ordered_items"):
            payload["ordered_items"] = enrich_component_lines(db, payload["ordered_items"])
        if payload.get("receipt_history"):
            payload["receipt_history"] = [
                {
                    **entry,
                    "items": enrich_component_lines(db, entry.get("items") or []),
                }
                for entry in payload["receipt_history"]
            ]
        transfer_id = payload.get("material_transfer_id")
        if transfer_id:
            transfer = db.query(MaterialTransfer).filter(MaterialTransfer.id == int(transfer_id)).first()
            if transfer:
                payload["material_transfer"] = {
                    "id": transfer.id,
                    "status": transfer.status,
                    "recipient_role": transfer.recipient_role,
                    "source_task_id": transfer.source_task_id,
                    "source_task_type": transfer.source_task_type,
                    "issue_task_id": transfer.issue_task_id,
                    "receive_task_id": transfer.receive_task_id,
                    "lines": [
                        {
                            "component_id": line.component_id,
                            "line_uid": line.line_uid,
                            "requested_qty": line.requested_qty,
                            "reserved_qty": line.reserved_qty,
                            "issued_qty": line.issued_qty,
                            "accepted_qty": line.accepted_qty,
                        }
                        for line in transfer.lines
                    ],
                }
        if payload.get("assembly_assignments"):
            payload["assembly_assignments"] = [
                {
                    **item,
                    "user": _user_payload(db.get(User, item.get("user_id"))) if item.get("user_id") else None,
                }
                for item in payload["assembly_assignments"]
            ]
        if task.type == "assembly_planning":
            child_ids = [int(task_id) for task_id in (payload.get("child_task_ids") or [])]
            children = (
                db.query(WorkflowTask)
                .filter(WorkflowTask.id.in_(child_ids))
                .order_by(WorkflowTask.id.asc())
                .all()
                if child_ids
                else []
            )
            payload["assembly_allocations"] = [
                {
                    "task_id": child.id,
                    "status": child.status,
                    "assigned_user": _user_payload(db.get(User, child.assigned_user_id)) if child.assigned_user_id else None,
                    "product_name": ((child.payload or {}).get("product_context") or {}).get("product_name"),
                    "drawing_number": ((child.payload or {}).get("product_context") or {}).get("drawing_number"),
                    "planned_qty": float(
                        (child.payload or {}).get("planned_qty")
                        or ((child.payload or {}).get("product_context") or {}).get("qty")
                        or 0
                    ),
                    "produced_qty": sum(
                        float(entry.get("qty") or 0)
                        for entry in ((child.payload or {}).get("daily_progress") or [])
                    ),
                    "due_date": child.due_date,
                    "serial_numbers": list((child.payload or {}).get("serial_numbers") or []),
                }
                for child in children
            ]
        if task.type == "repair_defects":
            payload["open_material_flow"] = _open_material_flow_tasks(db, task.id)
    result = {
        "id": task.id,
        "order_id": task.order_id,
        "product_id": task.product_id,
        "type": task.type,
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "status": task.status,
        "assigned_user_id": task.assigned_user_id,
        "assigned_user": _user_payload(assigned_user),
        "created_by_user_id": task.created_by_user_id,
        "is_manual": bool(task.is_manual),
        "priority": task.priority or "normal",
        "payload": payload,
        "sort_order": task.sort_order or 0,
        "created_at": task.created_at,
        "due_date": task.due_date,
        "planned_start_at": task.planned_start_at,
        "estimated_minutes": task.estimated_minutes,
        "actual_minutes": task.actual_minutes,
        "sla_due_at": task.sla_due_at,
        "deadline_change_reason": task.deadline_change_reason,
        "hold_reason": task.hold_reason,
        "cancel_reason": task.cancel_reason,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "cancelled_at": task.cancelled_at,
        "updated_at": task.updated_at,
    }
    if db:
        result.update(task_runtime_payload(db, task))
        result["watcher_ids"] = [
            row.user_id for row in db.query(TaskWatcher).filter(TaskWatcher.task_id == task.id).all()
        ]
        result["dependencies"] = [
            {
                "id": row.id,
                "depends_on_task_id": row.depends_on_task_id,
                "dependency_type": row.dependency_type,
            }
            for row in db.query(TaskDependency).filter(TaskDependency.task_id == task.id).all()
        ]
    return result


def _can_access_task(task: WorkflowTask, user: User):
    if user_has_role(user, "admin", "manager", "production_manager"):
        return True
    if (
        (task.type == "tester_check" and user_has_role(user, "tester"))
        or (task.type == "assembler_build" and user_has_role(user, "assembler"))
    ):
        return True
    if task.type == "assembler_build":
        for assignment in (task.payload or {}).get("assembly_assignments") or []:
            if assignment.get("user_id") == user.id:
                return True
    if task.assigned_user_id:
        return task.assigned_user_id == user.id
    return task.role in user_roles(user)


@router.get("/{task_id}/form.xlsx")
def download_material_task_form(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if not _can_access_task(task, user):
        raise HTTPException(status_code=403, detail="Эта задача назначена другой роли")
    if task.type not in ["warehouse_issue_materials", "assembler_receive_materials", "repair_issue_materials", "repair_receive_materials"]:
        raise HTTPException(status_code=400, detail="Для этой задачи форма не предусмотрена")

    materials = enrich_component_lines(db, (task.payload or {}).get("materials") or [])
    is_issue = task.type not in ["assembler_receive_materials", "repair_receive_materials"]
    action = "Выдача" if is_issue else "Получение"
    rows = [[
        index,
        material.get("component_name") or f"Компонент ID {material['component_id']}",
        material.get("part_number") or "—",
        material.get("category") or "—",
        float(material.get("qty") or 0),
        "",
        "",
    ] for index, material in enumerate(materials, start=1)]
    workbook = build_table_xlsx(
        sheet_name=action,
        title=f"Форма: {action.lower()} комплектующих",
        metadata=[("Заказ", f"№ {task.order_id}"), ("Задача", f"№ {task.id}"), ("Статус", task.status)],
        headers=["№", "Наименование", "Артикул", "Категория", "По заданию", "Фактически", "Примечание / подпись"],
        rows=rows,
        widths=[7, 46, 28, 24, 14, 14, 30],
    )
    filename = f"{action} комплектующих заказ {task.order_id}.xlsx"
    return StreamingResponse(
        workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def _can_work_task(task: WorkflowTask, user: User):
    if task.type == "assembler_build":
        for assignment in (task.payload or {}).get("assembly_assignments") or []:
            if assignment.get("user_id") == user.id:
                return True
    if task.type == "tester_check" and user_has_role(user, "tester"):
        return True
    if task.type == "assembler_build" and user_has_role(user, "assembler"):
        return True
    return user_has_role(user, "admin", "manager", "production_manager") or task.assigned_user_id == user.id


def _can_manage_tasks(user: User) -> bool:
    return user_has_role(user, "admin", "manager", "production_manager")


def _parse_datetime(value: str | None, field_name: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Некорректное поле {field_name}")


def _task_or_404(db: Session, task_id: int) -> WorkflowTask:
    task = db.get(WorkflowTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return task


def _assignee_or_404(db: Session, user_id: int | None, role: str) -> User | None:
    if not user_id:
        return None
    assignee = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not assignee:
        raise HTTPException(status_code=404, detail="Сотрудник не найден")
    if role not in user_roles(assignee) and not user_has_role(assignee, "admin", "manager", "production_manager"):
        raise HTTPException(status_code=422, detail="Роль сотрудника не совпадает с ролью задачи")
    return assignee


def _apply_status_filter(query, status: str):
    archive_cutoff = utcnow() - timedelta(days=1)
    if status == "all":
        return query
    if status == "archive":
        return query.filter(
            WorkflowTask.status == "done",
            or_(WorkflowTask.completed_at < archive_cutoff, WorkflowTask.completed_at.is_(None)),
        )
    if status in ["active", "open"]:
        return query.filter(or_(
            WorkflowTask.status.in_(["assigned", "in_progress", "open", "waiting_delivery", "hold", "ready_to_issue"]),
            (WorkflowTask.status == "done") & (WorkflowTask.completed_at >= archive_cutoff),
        ))
    return query.filter(WorkflowTask.status == status)


def _task_expected_dates(task: WorkflowTask):
    payload = task.payload or {}
    dates = []
    if payload.get("expected_date"):
        dates.append(payload.get("expected_date"))
    for item in payload.get("shortages") or []:
        if item.get("expected_date"):
            dates.append(item.get("expected_date"))
    for item in payload.get("purchases") or []:
        if float(item.get("received_qty") or 0) < float(item.get("qty") or 0) and item.get("expected_date"):
            dates.append(item.get("expected_date"))
    return dates


def _is_past_date(value: str):
    try:
        return datetime.fromisoformat(value).date() < utcnow().date()
    except ValueError:
        return False


def _task_kanban_column(task: WorkflowTask):
    if task.status != "done" and any(_is_past_date(value) for value in _task_expected_dates(task)):
        return "delayed"
    if task.status != "done" and task.type == "warehouse_receive_components" and _task_expected_dates(task):
        return "waiting_delivery"
    if task.status in ["assigned", "open"]:
        return "assigned"
    if task.status == "hold":
        return "hold"
    if task.status == "waiting_delivery":
        return "waiting_delivery"
    if task.status == "ready_to_issue":
        return "in_progress"
    if task.status == "done":
        return "done"
    return "in_progress"


def _safe_filename(filename: str):
    cleaned = re.sub(r"[^A-Za-zА-Яа-я0-9._-]+", "_", filename).strip("._")
    return cleaned or "file"


@router.get("/mine")
def get_my_tasks(
    status: str = "active",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    split_aggregate_assembly_tasks(db)
    reconcile_stock_reservations(db)
    ensure_missing_order_item_workflows(db)
    merge_order_procurement_tasks(db)
    reconcile_procurement_tasks_with_stock(db)
    ensure_procurement_payment_tasks(db)
    cleanup_premature_assembly_tasks(db)
    ensure_assembly_tasks_after_receipts(db)
    db.flush()
    query = db.query(WorkflowTask)
    if not user_has_role(user, "admin"):
        roles = user_roles(user)
        role_conditions = [WorkflowTask.role.in_(roles), WorkflowTask.type == "assembler_build"]
        assignee_conditions = [
            WorkflowTask.assigned_user_id.is_(None),
            WorkflowTask.assigned_user_id == user.id,
            WorkflowTask.type == "assembler_build",
            WorkflowTask.type == "tester_check",
        ]
        query = query.filter(
            or_(*role_conditions),
            or_(*assignee_conditions),
        )
    query = _apply_status_filter(query, status)
    tasks = query.order_by(WorkflowTask.sort_order.asc(), WorkflowTask.created_at.desc()).all()
    result = [_task_payload(task, db) for task in tasks if _can_access_task(task, user)]
    db.commit()
    return result


@router.get("")
def get_all_tasks(
    status: str = "active",
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin", "manager", "production_manager")),
):
    split_aggregate_assembly_tasks(db)
    reconcile_stock_reservations(db)
    ensure_missing_order_item_workflows(db)
    merge_order_procurement_tasks(db)
    reconcile_procurement_tasks_with_stock(db)
    ensure_procurement_payment_tasks(db)
    cleanup_premature_assembly_tasks(db)
    ensure_assembly_tasks_after_receipts(db)
    db.flush()
    query = db.query(WorkflowTask)
    query = _apply_status_filter(query, status)
    tasks = query.order_by(WorkflowTask.sort_order.asc(), WorkflowTask.created_at.desc()).all()
    result = [_task_payload(task, db) for task in tasks]
    db.commit()
    return result


@router.get("/assignees")
def list_task_assignees(
    role: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    query = db.query(User).filter(User.is_active == True)
    users = query.order_by(User.full_name, User.username).all()
    if role:
        users = [user for user in users if role in user_roles(user) or user_has_role(user, "admin", "manager")]
    return [_user_payload(user) for user in users]


@router.get("/manual-options")
def get_manual_task_options(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Справочники для формы ручной задачи без ввода внутренних ID."""
    orders = db.query(Order).order_by(Order.id.desc()).all()
    products = db.query(ProductType).order_by(ProductType.name.asc()).all()
    active_tasks = db.query(WorkflowTask).filter(
        WorkflowTask.status.notin_(["done", "cancelled", "merged"]),
    ).order_by(WorkflowTask.created_at.desc()).all()
    if not _can_manage_tasks(user):
        active_tasks = [task for task in active_tasks if _can_access_task(task, user)]
    return {
        "orders": [
            {
                "id": order.id,
                "customer_name": order.customer_name,
                "status": order.status,
            }
            for order in orders
        ],
        "products": [
            {
                "id": product.id,
                "name": product.name,
                "drawing_number": product.drawing_number,
                "sku": product.sku,
            }
            for product in products
        ],
        "tasks": [
            {
                "id": task.id,
                "title": task.title,
                "role": task.role,
                "status": task.status,
            }
            for task in active_tasks
        ],
    }


@router.post("/manual")
def create_manual_task(
    payload: ManualTaskCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Название задачи обязательно")
    if payload.priority not in TASK_PRIORITIES:
        raise HTTPException(status_code=422, detail="Некорректный приоритет")
    if not _can_manage_tasks(user) and payload.role not in user_roles(user):
        raise HTTPException(status_code=403, detail="Можно создавать задачи только внутри своего отдела")
    if payload.order_id and not db.get(Order, payload.order_id):
        raise HTTPException(status_code=404, detail="Производственный заказ не найден")
    if payload.product_id and not db.get(ProductType, payload.product_id):
        raise HTTPException(status_code=404, detail="Изделие не найдено")

    assignee = _assignee_or_404(db, payload.assigned_user_id, payload.role)
    planned_start = _parse_datetime(payload.planned_start_at, "planned_start_at")
    due_date = _parse_datetime(payload.due_date, "due_date")
    if payload.estimated_minutes is not None and payload.estimated_minutes <= 0:
        raise HTTPException(status_code=422, detail="Оценка должна быть больше нуля")

    task = WorkflowTask(
        order_id=payload.order_id,
        product_id=payload.product_id,
        type="manual",
        title=title,
        description=(payload.description or "").strip() or None,
        role=payload.role,
        status="assigned" if assignee else "open",
        assigned_user_id=assignee.id if assignee else None,
        created_by_user_id=user.id,
        is_manual=True,
        priority=payload.priority,
        planned_start_at=planned_start,
        due_date=due_date,
        estimated_minutes=payload.estimated_minutes,
        sla_due_at=calculate_sla_due(payload.priority, planned_start or utcnow()),
        payload={},
    )
    db.add(task)
    db.flush()
    add_watcher(db, task, user.id, user.id)
    if assignee and assignee.id != user.id:
        add_watcher(db, task, assignee.id, user.id)
    for watcher_id in set(payload.watcher_ids):
        add_watcher(db, task, watcher_id, user.id)
    for dependency_id in set(payload.dependency_ids):
        ensure_dependency_is_valid(db, task.id, dependency_id, "blocks")
        db.add(TaskDependency(
            task_id=task.id,
            depends_on_task_id=dependency_id,
            dependency_type="blocks",
            created_by_user_id=user.id,
        ))
    record_event(
        db,
        task,
        "created",
        user.id,
        to_status=task.status,
        data={"manual": True, "priority": task.priority},
    )
    db.commit()
    return _task_payload(task, db)


@router.post("/bulk")
def bulk_update_tasks(
    payload: TaskBulkPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "manager", "production_manager")),
):
    task_ids = list(dict.fromkeys(payload.task_ids))
    if not task_ids:
        raise HTTPException(status_code=422, detail="Выберите задачи")
    tasks = db.query(WorkflowTask).filter(WorkflowTask.id.in_(task_ids)).all()
    if len(tasks) != len(task_ids):
        raise HTTPException(status_code=404, detail="Некоторые задачи не найдены")

    changed = []
    for task in tasks:
        if task.status in FINAL_STATUSES and payload.action != "reopen":
            raise HTTPException(status_code=409, detail=f"Задача #{task.id} уже закрыта")
        if payload.action == "assign":
            assignee = _assignee_or_404(db, payload.user_id, task.role)
            old_user_id = task.assigned_user_id
            task.assigned_user_id = assignee.id if assignee else None
            old_status = task.status
            task.status = "assigned" if assignee else "open"
            record_event(
                db, task, "assigned", user.id,
                from_status=old_status, to_status=task.status,
                data={"from_user_id": old_user_id, "to_user_id": task.assigned_user_id, "bulk": True},
            )
        elif payload.action == "deadline":
            reason = (payload.reason or "").strip()
            if not reason:
                raise HTTPException(status_code=422, detail="Укажите причину массового изменения срока")
            old_due = task.due_date
            task.due_date = _parse_datetime(payload.due_date, "due_date")
            task.deadline_change_reason = reason
            record_event(
                db, task, "deadline_changed", user.id, reason=reason,
                data={"from": old_due.isoformat() if old_due else None, "to": task.due_date.isoformat() if task.due_date else None, "bulk": True},
            )
        elif payload.action == "priority":
            if payload.priority not in TASK_PRIORITIES:
                raise HTTPException(status_code=422, detail="Некорректный приоритет")
            old_priority = task.priority
            task.priority = payload.priority
            task.sla_due_at = calculate_sla_due(task.priority, task.planned_start_at or task.created_at or utcnow())
            record_event(
                db, task, "priority_changed", user.id,
                data={"from": old_priority, "to": task.priority, "bulk": True},
            )
        elif payload.action == "cancel":
            transition_task(db, task, "cancelled", user, reason=payload.reason)
        elif payload.action == "reopen":
            transition_task(db, task, "open", user, reason=payload.reason)
        else:
            raise HTTPException(status_code=422, detail="Неизвестное массовое действие")
        changed.append(task.id)
    db.commit()
    return {"status": "success", "updated_ids": changed}


@router.post("/{task_id}/assembly-plan")
def create_assembly_plan(
    task_id: int,
    payload: AssemblyPlanPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "manager", "production_manager")),
):
    task = _task_or_404(db, task_id)
    if task.type != "assembler_build":
        raise HTTPException(status_code=409, detail="Распределять можно только задачу сборки")
    if task.status in FINAL_STATUSES:
        raise HTTPException(status_code=409, detail="Задача сборки уже закрыта")
    if not payload.allocations:
        raise HTTPException(status_code=422, detail="Добавьте хотя бы одного сборщика")

    source = normalize_assembly_task_payload(db, task)
    product_lines = source.get("product_lines") or []
    totals = {}
    prepared = []
    for allocation in payload.allocations:
        if allocation.quantity <= 0 or not float(allocation.quantity).is_integer():
            raise HTTPException(status_code=422, detail="Количество изделий в партии должно быть целым числом больше нуля")
        assembler = _assignee_or_404(db, allocation.user_id, "assembler")
        if "assembler" not in user_roles(assembler) and not user_has_role(assembler, "admin"):
            raise HTTPException(status_code=422, detail="Для партии нужно выбрать сотрудника с ролью сборщика")
        line = next(
            (
                item for item in product_lines
                if (
                    allocation.order_item_id
                    and item.get("order_item_id") == allocation.order_item_id
                ) or (
                    allocation.product_id
                    and item.get("product_id") == allocation.product_id
                )
            ),
            None,
        )
        if not line:
            raise HTTPException(status_code=422, detail="Изделие для распределения не найдено")
        key = line.get("order_item_id") or line.get("product_id")
        totals[key] = totals.get(key, 0) + allocation.quantity
        prepared.append((allocation, assembler, line))

    for line in product_lines:
        key = line.get("order_item_id") or line.get("product_id")
        if totals.get(key, 0) > float(line.get("qty") or 0):
            raise HTTPException(
                status_code=422,
                detail=f"Для изделия «{line.get('product_name') or key}» распределено больше количества заказа",
            )

    grouped_allocations = {}
    for allocation, assembler, line in prepared:
        key = line.get("order_item_id") or line.get("product_id")
        grouped_allocations.setdefault(key, {"line": line, "allocations": []})["allocations"].append((allocation, assembler))

    children = []
    for group in grouped_allocations.values():
        line = group["line"]
        line_allocations = group["allocations"]
        group_quantity = int(sum(allocation.quantity for allocation, _ in line_allocations))
        context = {
            "order_item_id": line.get("order_item_id"),
            "product_id": line.get("product_id"),
            "product_name": line.get("product_name"),
            "drawing_number": line.get("drawing_number"),
            "qty": group_quantity,
        }
        child = WorkflowTask(
            order_id=task.order_id,
            product_id=line.get("product_id"),
            type="assembler_build",
            title=f"Собрать {group_quantity:g} шт. · {line.get('product_name') or 'изделие'}",
            description="Общая очередь сборки. Сборщики закрепляют за собой конкретные заводские номера и передают готовые устройства на тестирование.",
            role="assembler",
            status="assigned",
            assigned_user_id=None,
            created_by_user_id=user.id,
            priority=task.priority or "normal",
            due_date=min(
                (
                    _parse_datetime(allocation.due_date, "due_date")
                    for allocation, _ in line_allocations
                    if allocation.due_date
                ),
                default=task.due_date,
            ),
            sla_due_at=task.sla_due_at,
            payload={
                "parent_planning_task_id": task.id,
                "product_context": context,
                "product_lines": [context],
                "planned_qty": group_quantity,
                "materials_complete": bool(source.get("materials_complete")),
                "issued_qty": group_quantity,
                "started_qty": 0,
                "daily_progress": [],
                "assembly_assignments": [
                    {
                        "id": str(uuid.uuid4()),
                        "order_item_id": line.get("order_item_id"),
                        "product_id": line.get("product_id"),
                        "product_name": line.get("product_name"),
                        "drawing_number": line.get("drawing_number"),
                        "user_id": assembler.id,
                        "planned_qty": allocation.quantity,
                        "produced_qty": 0,
                    }
                    for allocation, assembler in line_allocations
                ],
                "product_documents": source.get("product_documents") or [],
                "component_options": source.get("component_options") or [],
            },
        )
        db.add(child)
        db.flush()
        product = db.get(ProductType, line.get("product_id"))
        if not product:
            raise HTTPException(status_code=422, detail="Карточка изделия для заводских номеров не найдена")
        units = create_product_units(
            db,
            order_id=task.order_id,
            order_item_id=line.get("order_item_id"),
            product=product,
            assembly_task_id=child.id,
            assigned_user_id=line_allocations[0][1].id,
            quantity=group_quantity,
        )
        assembly_claims = {}
        unit_index = 0
        for allocation, assembler in line_allocations:
            for unit in units[unit_index:unit_index + int(allocation.quantity)]:
                unit.assigned_user_id = assembler.id
                assembly_claims[unit.serial_number] = assembler.id
            unit_index += int(allocation.quantity)
        child.payload = {
            **child.payload,
            "unit_ids": [unit.id for unit in units],
            "serial_numbers": [unit.serial_number for unit in units],
            "assembly_claims": assembly_claims,
        }
        record_event(
            db, child, "created_from_assembly_plan", user.id, to_status="assigned",
            data={
                "parent_task_id": task.id,
                "planned_qty": group_quantity,
                "serial_numbers": [unit.serial_number for unit in units],
            },
        )
        children.append(child)

    previous_status = task.status
    task.type = "assembly_planning"
    task.title = f"Распределение сборки по заказу #{task.order_id} · {(source.get('product_context') or {}).get('product_name') or 'изделие'}"
    task.description = "План выпуска сформирован. Для каждого изделия создана общая очередь сборки с первоначальным закреплением заводских номеров за сборщиками."
    task.role = "production_manager"
    task.status = "done"
    task.completed_at = utcnow()
    task.payload = {
        **source,
        "child_task_ids": [child.id for child in children],
        "planned_by_user_id": user.id,
        "planned_at": utcnow().isoformat(),
    }
    record_event(
        db, task, "assembly_distributed", user.id,
        from_status=previous_status, to_status="done",
        data={"child_task_ids": [child.id for child in children]},
    )
    db.commit()
    return {
        "planning_task": _task_payload(task, db),
        "tasks": [_task_payload(child, db) for child in children],
    }


@router.get("/assembly/daily-summary")
def get_assembly_daily_summary(
    date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin", "manager", "production_manager")),
):
    target_date = date or utcnow().date().isoformat()
    tasks = db.query(WorkflowTask).filter(
        WorkflowTask.type == "assembler_build",
        WorkflowTask.status.notin_(["cancelled", "merged"]),
    ).all()
    rows = []
    for task in tasks:
        task_payload = task.payload or {}
        assignments = task_payload.get("assembly_assignments") or [{
            "user_id": task.assigned_user_id,
            "planned_qty": task_payload.get("planned_qty") or (task_payload.get("product_context") or {}).get("qty") or 0,
            "produced_qty": sum(float(entry.get("qty") or 0) for entry in task_payload.get("daily_progress") or []),
            "product_name": (task_payload.get("product_context") or {}).get("product_name"),
        }]
        progress = task_payload.get("daily_progress") or []
        for assignment in assignments:
            user_id = assignment.get("user_id") or task.assigned_user_id
            if not user_id:
                continue
            relevant = [
                entry for entry in progress
                if entry.get("date") == target_date
                and (not entry.get("user_id") or int(entry.get("user_id")) == int(user_id))
                and (not assignment.get("id") or not entry.get("assignment_id") or entry.get("assignment_id") == assignment.get("id"))
            ]
            today_qty = sum(float(entry.get("qty") or 0) for entry in relevant)
            total_qty = float(assignment.get("produced_qty") or 0)
            if not total_qty:
                total_qty = sum(
                    float(entry.get("qty") or 0)
                    for entry in progress
                    if not entry.get("user_id") or int(entry.get("user_id")) == int(user_id)
                )
            planned_qty = float(assignment.get("planned_qty") or 0)
            worker = db.get(User, user_id)
            rows.append({
                "task_id": task.id,
                "order_id": task.order_id,
                "user_id": user_id,
                "user_name": worker.full_name or worker.username if worker else f"Сотрудник #{user_id}",
                "product_name": assignment.get("product_name") or (task_payload.get("product_context") or {}).get("product_name") or "Изделие",
                "planned_qty": planned_qty,
                "today_qty": today_qty,
                "total_qty": total_qty,
                "remaining_qty": max(planned_qty - total_qty, 0),
                "status": task.status,
                "has_daily_report": bool(relevant),
            })
    return {
        "date": target_date,
        "rows": sorted(rows, key=lambda item: (item["user_name"], item["order_id"] or 0, item["task_id"])),
        "today_total": sum(item["today_qty"] for item in rows),
        "without_report": sum(1 for item in rows if not item["has_daily_report"] and item["status"] == "in_progress"),
    }


@router.get("/notifications/mine")
def get_my_task_notifications(
    unread_only: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(TaskNotification).filter(TaskNotification.user_id == user.id)
    if unread_only:
        query = query.filter(TaskNotification.read_at.is_(None))
    rows = query.order_by(TaskNotification.created_at.desc()).limit(100).all()
    events = {
        event.id: event
        for event in db.query(TaskEvent).filter(TaskEvent.id.in_([row.event_id for row in rows])).all()
    } if rows else {}
    return [{
        "id": row.id,
        "task_id": row.task_id,
        "read_at": row.read_at,
        "created_at": row.created_at,
        "event": {
            "type": events[row.event_id].event_type,
            "reason": events[row.event_id].reason,
            "data": events[row.event_id].data or {},
        } if row.event_id in events else None,
    } for row in rows]


@router.post("/notifications/{notification_id}/read")
def read_task_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = db.query(TaskNotification).filter_by(id=notification_id, user_id=user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Уведомление не найдено")
    row.read_at = row.read_at or utcnow()
    db.commit()
    return {"status": "success"}


@router.get("/{task_id}")
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if not _can_access_task(task, user):
        raise HTTPException(status_code=403, detail="Эта задача назначена другой роли")
    result = _task_payload(task, db)
    db.commit()
    return result


@router.patch("/{task_id}")
def update_task(
    task_id: int,
    payload: TaskUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = _task_or_404(db, task_id)
    if not _can_manage_tasks(user) and task.created_by_user_id != user.id:
        raise HTTPException(status_code=403, detail="Изменять задачу может автор или руководитель")
    if task.status in FINAL_STATUSES and not _can_manage_tasks(user):
        raise HTTPException(status_code=403, detail="Закрытую задачу может менять только менеджер")
    changes = {}

    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="Название задачи обязательно")
        changes["title"] = [task.title, title]
        task.title = title
    if payload.description is not None:
        value = payload.description.strip() or None
        changes["description"] = [task.description, value]
        task.description = value
    if payload.role is not None and payload.role != task.role:
        if not _can_manage_tasks(user) and payload.role not in user_roles(user):
            raise HTTPException(status_code=403, detail="Нельзя перенести задачу в другой отдел")
        changes["role"] = [task.role, payload.role]
        task.role = payload.role
        if task.assigned_user_id:
            _assignee_or_404(db, task.assigned_user_id, task.role)
    if payload.priority is not None and payload.priority != task.priority:
        if payload.priority not in TASK_PRIORITIES:
            raise HTTPException(status_code=422, detail="Некорректный приоритет")
        changes["priority"] = [task.priority, payload.priority]
        task.priority = payload.priority
        task.sla_due_at = calculate_sla_due(task.priority, task.planned_start_at or task.created_at or utcnow())
    if payload.planned_start_at is not None:
        value = _parse_datetime(payload.planned_start_at, "planned_start_at")
        changes["planned_start_at"] = [
            task.planned_start_at.isoformat() if task.planned_start_at else None,
            value.isoformat() if value else None,
        ]
        task.planned_start_at = value
    if payload.due_date is not None:
        reason = (payload.reason or "").strip()
        if not reason:
            raise HTTPException(status_code=422, detail="Укажите причину изменения срока")
        value = _parse_datetime(payload.due_date, "due_date")
        changes["due_date"] = [
            task.due_date.isoformat() if task.due_date else None,
            value.isoformat() if value else None,
        ]
        task.due_date = value
        task.deadline_change_reason = reason
    if payload.estimated_minutes is not None:
        if payload.estimated_minutes <= 0:
            raise HTTPException(status_code=422, detail="Оценка должна быть больше нуля")
        changes["estimated_minutes"] = [task.estimated_minutes, payload.estimated_minutes]
        task.estimated_minutes = payload.estimated_minutes
    if payload.actual_minutes is not None:
        if payload.actual_minutes < 0:
            raise HTTPException(status_code=422, detail="Фактическое время не может быть отрицательным")
        changes["actual_minutes"] = [task.actual_minutes, payload.actual_minutes]
        task.actual_minutes = payload.actual_minutes
    if changes:
        record_event(
            db, task, "updated", user.id,
            reason=(payload.reason or "").strip() or None,
            data={"changes": changes},
        )
    db.commit()
    return _task_payload(task, db)


@router.post("/{task_id}/transition")
def transition_workflow_task(
    task_id: int,
    payload: TaskTransitionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = _task_or_404(db, task_id)
    if not _can_access_task(task, user):
        raise HTTPException(status_code=403, detail="Нет доступа к задаче")
    if payload.status == "cancelled" and not _can_manage_tasks(user):
        raise HTTPException(status_code=403, detail="Отменять задачи может только менеджер или руководитель")
    transition_task(db, task, payload.status, user, reason=payload.reason)
    db.commit()
    return _task_payload(task, db)


@router.get("/{task_id}/events")
def get_task_events(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = _task_or_404(db, task_id)
    if not _can_access_task(task, user):
        raise HTTPException(status_code=403, detail="Нет доступа к задаче")
    events = db.query(TaskEvent).filter(TaskEvent.task_id == task.id).order_by(TaskEvent.created_at.desc()).all()
    actor_ids = {event.actor_user_id for event in events if event.actor_user_id}
    actors = {
        actor.id: _user_payload(actor)
        for actor in db.query(User).filter(User.id.in_(actor_ids)).all()
    } if actor_ids else {}
    return [{
        "id": event.id,
        "event_type": event.event_type,
        "actor": actors.get(event.actor_user_id),
        "from_status": event.from_status,
        "to_status": event.to_status,
        "reason": event.reason,
        "data": event.data or {},
        "created_at": event.created_at,
    } for event in events]


@router.post("/{task_id}/watchers")
def create_task_watcher(
    task_id: int,
    payload: TaskWatcherPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = _task_or_404(db, task_id)
    if not _can_access_task(task, user):
        raise HTTPException(status_code=403, detail="Нет доступа к задаче")
    watcher = add_watcher(db, task, payload.user_id, user.id)
    db.commit()
    return {"id": watcher.id, "task_id": watcher.task_id, "user_id": watcher.user_id}


@router.delete("/{task_id}/watchers/{user_id}")
def delete_task_watcher(
    task_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = _task_or_404(db, task_id)
    if user.id != user_id and not _can_manage_tasks(user):
        raise HTTPException(status_code=403, detail="Можно удалить только себя из наблюдателей")
    watcher = db.query(TaskWatcher).filter_by(task_id=task.id, user_id=user_id).first()
    if not watcher:
        raise HTTPException(status_code=404, detail="Наблюдатель не найден")
    db.delete(watcher)
    record_event(db, task, "watcher_removed", user.id, data={"user_id": user_id})
    db.commit()
    return {"status": "success"}


@router.post("/{task_id}/dependencies")
def create_task_dependency(
    task_id: int,
    payload: TaskDependencyPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = _task_or_404(db, task_id)
    if not _can_manage_tasks(user) and task.created_by_user_id != user.id:
        raise HTTPException(status_code=403, detail="Управлять зависимостями может автор или руководитель")
    ensure_dependency_is_valid(db, task.id, payload.depends_on_task_id, payload.dependency_type)
    existing = db.query(TaskDependency).filter_by(
        task_id=task.id,
        depends_on_task_id=payload.depends_on_task_id,
        dependency_type=payload.dependency_type,
    ).first()
    if existing:
        return {"id": existing.id, "status": "exists"}
    row = TaskDependency(
        task_id=task.id,
        depends_on_task_id=payload.depends_on_task_id,
        dependency_type=payload.dependency_type,
        created_by_user_id=user.id,
    )
    db.add(row)
    db.flush()
    record_event(
        db, task, "dependency_added", user.id,
        data={"depends_on_task_id": payload.depends_on_task_id, "dependency_type": payload.dependency_type},
    )
    db.commit()
    return {"id": row.id, "status": "created"}


@router.delete("/{task_id}/dependencies/{dependency_id}")
def delete_task_dependency(
    task_id: int,
    dependency_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = _task_or_404(db, task_id)
    if not _can_manage_tasks(user) and task.created_by_user_id != user.id:
        raise HTTPException(status_code=403, detail="Управлять зависимостями может автор или руководитель")
    row = db.query(TaskDependency).filter_by(id=dependency_id, task_id=task.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Зависимость не найдена")
    data = {"depends_on_task_id": row.depends_on_task_id, "dependency_type": row.dependency_type}
    db.delete(row)
    record_event(db, task, "dependency_removed", user.id, data=data)
    db.commit()
    return {"status": "success"}


@router.post("/{task_id}/take")
def take_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.status == "done":
        raise HTTPException(status_code=400, detail="Задача уже закрыта")
    if task.status == "waiting_delivery":
        raise HTTPException(status_code=400, detail="Задача ожидает поставку и не может быть повторно взята в работу")
    if task.type == "tester_check" and user_has_role(user, "tester"):
        previous_status = task.status
        task.status = "in_progress"
        task.started_at = task.started_at or utcnow()
        record_event(
            db, task, "shared_testing_started", user.id,
            from_status=previous_status, to_status="in_progress",
        )
        db.commit()
        return _task_payload(task, db)
    if task.status == "in_progress" and task.assigned_user_id == user.id:
        raise HTTPException(status_code=400, detail="Задача уже в работе у текущего пользователя")
    if task.assigned_user_id and task.assigned_user_id != user.id:
        raise HTTPException(status_code=400, detail="Задача уже назначена другому сотруднику")
    if not user_has_role(user, "admin", "manager", "production_manager") and task.role not in user_roles(user):
        raise HTTPException(status_code=403, detail="Эта задача назначена другой роли")

    ensure_not_blocked(db, task)
    previous_status = task.status
    task.assigned_user_id = user.id
    task.status = "in_progress"
    task.started_at = task.started_at or utcnow()
    record_event(
        db, task, "started", user.id,
        from_status=previous_status, to_status="in_progress",
        data={"assigned_user_id": user.id},
    )
    db.commit()
    return _task_payload(task, db)


@router.post("/{task_id}/testing-claims")
def update_testing_claims(
    task_id: int,
    request: TestingClaimsPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = (
        db.query(WorkflowTask)
        .filter(WorkflowTask.id == task_id)
        .with_for_update()
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.type != "tester_check":
        raise HTTPException(status_code=409, detail="Устройства можно закреплять только в задаче тестирования")
    if not user_has_role(user, "tester", "admin", "manager", "production_manager"):
        raise HTTPException(status_code=403, detail="Закреплять устройства может только тестировщик")
    if request.action not in ["claim", "release"]:
        raise HTTPException(status_code=422, detail="Неизвестное действие с устройствами")

    requested_serials = {str(item).strip() for item in request.serial_numbers if str(item).strip()}
    if not requested_serials:
        raise HTTPException(status_code=422, detail="Выберите хотя бы одно устройство")
    payload = dict(task.payload or {})
    available_units = (
        db.query(Item)
        .filter(
            Item.id.in_(payload.get("unit_ids") or [-1]),
            Item.status == "testing",
        )
        .all()
    )
    available_serials = {unit.serial_number for unit in available_units}
    unknown_serials = requested_serials - available_serials
    if unknown_serials:
        raise HTTPException(status_code=409, detail="Часть устройств уже обработана или отсутствует в задаче")

    claims = {
        str(serial_number): int(user_id)
        for serial_number, user_id in (payload.get("testing_claims") or {}).items()
        if serial_number in available_serials
    }
    if request.action == "claim":
        conflicts = [
            serial_number for serial_number in requested_serials
            if claims.get(serial_number) not in [None, user.id]
        ]
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail=f"Устройства уже взяты другим тестировщиком: {', '.join(sorted(conflicts))}",
            )
        for serial_number in requested_serials:
            claims[serial_number] = user.id
    else:
        can_manage = user_has_role(user, "admin", "manager", "production_manager")
        forbidden = [
            serial_number for serial_number in requested_serials
            if claims.get(serial_number) not in [None, user.id] and not can_manage
        ]
        if forbidden:
            raise HTTPException(status_code=403, detail="Нельзя освободить устройства другого тестировщика")
        for serial_number in requested_serials:
            claims.pop(serial_number, None)

    task.payload = {**payload, "testing_claims": claims}
    task.status = "in_progress"
    task.started_at = task.started_at or utcnow()
    record_event(
        db, task, f"testing_devices_{request.action}ed", user.id,
        data={"serial_numbers": sorted(requested_serials)},
    )
    db.commit()
    return _task_payload(task, db)


@router.post("/{task_id}/assembly-claims")
def update_assembly_claims(
    task_id: int,
    request: TestingClaimsPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).with_for_update().first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.type != "assembler_build":
        raise HTTPException(status_code=409, detail="Устройства можно закреплять только в задаче сборки")
    if not user_has_role(user, "assembler", "admin", "manager", "production_manager"):
        raise HTTPException(status_code=403, detail="Закреплять устройства может только сборщик")
    if request.action not in ["claim", "release"]:
        raise HTTPException(status_code=422, detail="Неизвестное действие с устройствами")
    requested_serials = {str(item).strip() for item in request.serial_numbers if str(item).strip()}
    if not requested_serials:
        raise HTTPException(status_code=422, detail="Выберите хотя бы одно устройство")

    payload = dict(task.payload or {})
    available_units = db.query(Item).filter(
        Item.id.in_(payload.get("unit_ids") or [-1]),
        Item.status.in_(["planned", "in_assembly"]),
    ).all()
    available_serials = {unit.serial_number for unit in available_units}
    if requested_serials - available_serials:
        raise HTTPException(status_code=409, detail="Часть устройств уже собрана или отсутствует в задаче")
    stored_claims = payload.get("assembly_claims")
    if stored_claims is None and task.assigned_user_id:
        stored_claims = {serial_number: task.assigned_user_id for serial_number in available_serials}
    claims = {
        str(serial_number): int(user_id)
        for serial_number, user_id in (stored_claims or {}).items()
        if serial_number in available_serials
    }
    if request.action == "claim":
        conflicts = [serial_number for serial_number in requested_serials if claims.get(serial_number) not in [None, user.id]]
        if conflicts:
            raise HTTPException(status_code=409, detail=f"Устройства уже взяты другим сборщиком: {', '.join(sorted(conflicts))}")
        for serial_number in requested_serials:
            claims[serial_number] = user.id
    else:
        can_manage = user_has_role(user, "admin", "manager", "production_manager")
        if any(claims.get(serial_number) not in [None, user.id] for serial_number in requested_serials) and not can_manage:
            raise HTTPException(status_code=403, detail="Нельзя освободить устройства другого сборщика")
        for serial_number in requested_serials:
            claims.pop(serial_number, None)
    units_by_serial = {unit.serial_number: unit for unit in available_units}
    for serial_number in requested_serials:
        units_by_serial[serial_number].assigned_user_id = claims.get(serial_number)

    task.payload = {**payload, "assembly_claims": claims}
    task.status = "in_progress"
    task.started_at = task.started_at or utcnow()
    record_event(db, task, f"assembly_devices_{request.action}ed", user.id, data={"serial_numbers": sorted(requested_serials)})
    db.commit()
    return _task_payload(task, db)


@router.post("/{task_id}/assign")
def assign_task(
    task_id: int,
    payload: TaskAssignPayload,
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles("admin", "manager", "production_manager")),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.status == "done":
        raise HTTPException(status_code=400, detail="Задача уже закрыта")

    assignee = _assignee_or_404(db, payload.user_id, task.role)
    old_user_id = task.assigned_user_id
    previous_status = task.status
    task.assigned_user_id = assignee.id if assignee else None
    task.status = "assigned" if assignee else "open"
    task.started_at = None
    record_event(
        db, task, "assigned", actor.id,
        from_status=previous_status, to_status=task.status,
        data={"from_user_id": old_user_id, "to_user_id": task.assigned_user_id},
    )
    db.commit()
    return _task_payload(task, db)


@router.post("/{task_id}/deadline")
def set_task_deadline(
    task_id: int,
    payload: TaskDeadlinePayload,
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles("admin", "manager", "production_manager")),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.status == "done":
        raise HTTPException(status_code=400, detail="Задача уже закрыта")

    reason = (payload.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=422, detail="Укажите причину изменения срока")
    old_due = task.due_date
    task.due_date = _parse_datetime(payload.due_date, "due_date")
    task.deadline_change_reason = reason
    record_event(
        db, task, "deadline_changed", actor.id, reason=reason,
        data={
            "from": old_due.isoformat() if old_due else None,
            "to": task.due_date.isoformat() if task.due_date else None,
        },
    )

    db.commit()
    return _task_payload(task, db)


@router.post("/{task_id}/hold")
def hold_task(
    task_id: int,
    payload: TaskTransitionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if not _can_access_task(task, user):
        raise HTTPException(status_code=403, detail="Эта задача назначена другой роли")
    if task.status == "hold":
        return _task_payload(task, db)
    previous_status = task.status
    task_payload = task.payload or {}
    task.payload = {**task_payload, "previous_status": task.status}
    transition_task(db, task, "hold", user, reason=payload.reason)
    task.payload = {**(task.payload or {}), "previous_status": previous_status}
    db.commit()
    return _task_payload(task, db)


@router.post("/{task_id}/resume")
def resume_task(
    task_id: int,
    payload: TaskTransitionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if not _can_access_task(task, user):
        raise HTTPException(status_code=403, detail="Эта задача назначена другой роли")
    if task.status != "hold":
        return _task_payload(task, db)

    task_payload = task.payload or {}
    previous_status = task_payload.get("previous_status") or ("in_progress" if task.assigned_user_id else "assigned")
    if previous_status not in ["assigned", "in_progress", "open", "waiting_delivery"]:
        previous_status = "assigned"
    transition_task(db, task, previous_status, user, reason=payload.reason)
    task.payload = {key: value for key, value in task_payload.items() if key != "previous_status"}
    db.commit()
    return _task_payload(task, db)


@router.post("/kanban/reorder")
def reorder_tasks(
    payload: TaskReorderPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not payload.ordered_ids:
        return {"status": "success"}

    base_query = db.query(WorkflowTask)
    if not user_has_role(user, "admin"):
        roles = user_roles(user)
        role_conditions = [WorkflowTask.role.in_(roles)]
        assignee_conditions = [
            WorkflowTask.assigned_user_id.is_(None),
            WorkflowTask.assigned_user_id == user.id,
        ]
        base_query = base_query.filter(
            or_(*role_conditions),
            or_(*assignee_conditions),
        )
    tasks = _apply_status_filter(base_query, "active").all()
    if payload.column:
        tasks = [task for task in tasks if _task_kanban_column(task) == payload.column]

    tasks_by_id = {task.id: task for task in tasks if _can_access_task(task, user)}
    ordered_ids = [task_id for task_id in payload.ordered_ids if task_id in tasks_by_id]
    remaining_ids = [
        task.id
        for task in sorted(tasks_by_id.values(), key=lambda item: (item.sort_order or 0, item.created_at or datetime.min), reverse=False)
        if task.id not in ordered_ids
    ]

    for index, task_id in enumerate([*ordered_ids, *remaining_ids]):
        task = tasks_by_id.get(task_id)
        if task:
            task.sort_order = index

    db.commit()
    return {"status": "success"}


@router.post("/{task_id}/procurement-purchases")
def create_procurement_purchase(
    task_id: int,
    payload: ProcurementPurchasePayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if not _can_access_task(task, user):
        raise HTTPException(status_code=403, detail="Эта задача назначена другой роли")
    allowed_statuses = ["in_progress", "open", "assigned"] if task.type == "assembler_build" else ["in_progress", "open"]
    if task.status not in allowed_statuses or not _can_work_task(task, user):
        raise HTTPException(status_code=400, detail="Сначала задачу нужно взять в работу")

    result = add_procurement_purchase(db, task, payload.dict())
    db.commit()
    return {"status": "success", "task": _task_payload(task, db), "result": result}


@router.post("/{task_id}/notes")
def add_task_note(
    task_id: int,
    payload: TaskNotePayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if not _can_access_task(task, user):
        raise HTTPException(status_code=403, detail="Эта задача назначена другой роли")

    text = payload.note.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Комментарий не может быть пустым")
    task_payload = task.payload or {}
    notes = task_payload.get("notes", [])
    notes.append({
        "author_id": user.id,
        "author": user.username,
        "role": user.role,
        "roles": user_roles(user),
        "text": text,
        "created_at": utcnow().isoformat(),
    })
    task.payload = {**task_payload, "notes": notes}
    mentioned_user_ids = add_mentioned_watchers(db, task, text, user.id)
    record_event(
        db, task, "comment_added", user.id,
        data={"text": text, "mentioned_user_ids": mentioned_user_ids},
    )
    db.commit()
    return _task_payload(task, db)


@router.post("/{task_id}/attachments")
def upload_task_attachment(
    task_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if not _can_access_task(task, user):
        raise HTTPException(status_code=403, detail="Эта задача назначена другой роли")

    task_dir = UPLOAD_ROOT / str(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    original_name = _safe_filename(file.filename or "file")
    stored_name = f"{uuid.uuid4().hex}_{original_name}"
    target = task_dir / stored_name

    with target.open("wb") as out:
        while chunk := file.file.read(1024 * 1024):
            out.write(chunk)

    attachment = {
        "original_name": original_name,
        "stored_name": stored_name,
        "content_type": file.content_type,
        "url": f"/tasks/{task_id}/attachments/{stored_name}",
        "uploaded_by": user.username,
    }
    task_payload = task.payload or {}
    attachments = task_payload.get("attachments", [])
    attachments.append(attachment)
    task.payload = {**task_payload, "attachments": attachments}
    db.commit()
    return attachment


@router.get("/{task_id}/attachments/{stored_name}")
def download_task_attachment(
    task_id: int,
    stored_name: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if not _can_access_task(task, user):
        raise HTTPException(status_code=403, detail="Эта задача назначена другой роли")

    path = UPLOAD_ROOT / str(task_id) / stored_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path)


@router.post("/{task_id}/complete")
def complete_workflow_task(
    task_id: int,
    payload: TaskCompletePayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if not _can_access_task(task, user):
        raise HTTPException(status_code=403, detail="Эта задача назначена другой роли")
    request_body = payload.payload or {}
    request_hash = hashlib.sha256(
        json.dumps(request_body, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    if payload.idempotency_key:
        previous = db.query(WorkflowCommand).filter_by(
            task_id=task.id, idempotency_key=payload.idempotency_key,
        ).first()
        if previous:
            if previous.request_hash != request_hash:
                raise HTTPException(status_code=409, detail="Ключ повтора уже использован с другим содержимым")
            return previous.response_payload
    allowed_statuses = ["in_progress", "open"]
    if task.type in ["assembler_build", "tester_check"]:
        allowed_statuses.append("assigned")
    if task.status not in allowed_statuses or not _can_work_task(task, user):
        raise HTTPException(status_code=400, detail="Сначала задачу нужно взять в работу")

    ensure_not_blocked(db, task)
    previous_status = task.status
    result = complete_task(db, task, request_body, actor_user_id=user.id)
    record_event(
        db,
        task,
        "completed" if task.status == "done" else "progress_recorded",
        user.id,
        from_status=previous_status,
        to_status=task.status,
        data={"result_status": result.get("status") if isinstance(result, dict) else None},
    )
    response = {"status": "success", "task_id": task.id, "result": result}
    if payload.idempotency_key:
        db.add(WorkflowCommand(
            task_id=task.id,
            idempotency_key=payload.idempotency_key,
            command_type="complete",
            request_hash=request_hash,
            response_payload=response,
        ))
    db.commit()
    return response
