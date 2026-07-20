import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.auth import User
from app.models.production import WorkflowTask
from app.services.auth_service import get_current_user, require_roles, user_has_role, user_roles
from app.services.workflow_service import _open_material_flow_tasks, add_procurement_purchase, cleanup_premature_assembly_tasks, complete_task, enrich_component_lines, ensure_assembly_tasks_after_receipts, ensure_missing_order_item_workflows, ensure_procurement_payment_tasks, merge_order_procurement_tasks, normalize_assembly_task_payload, normalize_task_daily_progress, reconcile_procurement_tasks_with_stock, reconcile_stock_reservations, split_aggregate_assembly_tasks
from app.services.xlsx_service import build_table_xlsx

router = APIRouter(prefix="/tasks", tags=["Workflow задачи"])
UPLOAD_ROOT = Path("uploads/tasks")


class TaskCompletePayload(BaseModel):
    payload: Optional[dict] = None


class TaskNotePayload(BaseModel):
    note: str


class TaskAssignPayload(BaseModel):
    user_id: Optional[int] = None


class TaskDeadlinePayload(BaseModel):
    due_date: Optional[str] = None


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
        if payload.get("assembly_assignments"):
            payload["assembly_assignments"] = [
                {
                    **item,
                    "user": _user_payload(db.get(User, item.get("user_id"))) if item.get("user_id") else None,
                }
                for item in payload["assembly_assignments"]
            ]
        if task.type == "repair_defects":
            payload["open_material_flow"] = _open_material_flow_tasks(db, task.id)
    return {
        "id": task.id,
        "order_id": task.order_id,
        "type": task.type,
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "status": task.status,
        "assigned_user_id": task.assigned_user_id,
        "assigned_user": _user_payload(assigned_user),
        "payload": payload,
        "sort_order": task.sort_order or 0,
        "created_at": task.created_at,
        "due_date": task.due_date,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
    }


def _can_access_task(task: WorkflowTask, user: User):
    if user_has_role(user, "admin", "manager"):
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
    if task.type not in ["warehouse_issue_materials", "assembler_receive_materials", "repair_issue_materials"]:
        raise HTTPException(status_code=400, detail="Для этой задачи форма не предусмотрена")

    materials = enrich_component_lines(db, (task.payload or {}).get("materials") or [])
    is_issue = task.type != "assembler_receive_materials"
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
    return user_has_role(user, "admin", "manager") or task.assigned_user_id == user.id


def _apply_status_filter(query, status: str):
    archive_cutoff = datetime.utcnow() - timedelta(days=1)
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
        return datetime.fromisoformat(value).date() < datetime.utcnow().date()
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
    _: User = Depends(require_roles("admin", "manager")),
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
    if task.status == "in_progress" and task.assigned_user_id == user.id:
        raise HTTPException(status_code=400, detail="Задача уже в работе у текущего пользователя")
    if task.assigned_user_id and task.assigned_user_id != user.id:
        raise HTTPException(status_code=400, detail="Задача уже назначена другому сотруднику")
    if not user_has_role(user, "admin", "manager") and task.role not in user_roles(user):
        raise HTTPException(status_code=403, detail="Эта задача назначена другой роли")

    task.assigned_user_id = user.id
    task.status = "in_progress"
    task.started_at = task.started_at or datetime.utcnow()
    db.commit()
    return _task_payload(task, db)


@router.post("/{task_id}/assign")
def assign_task(
    task_id: int,
    payload: TaskAssignPayload,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin", "manager")),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.status == "done":
        raise HTTPException(status_code=400, detail="Задача уже закрыта")

    assignee = None
    if payload.user_id:
        assignee = db.query(User).filter(User.id == payload.user_id, User.is_active == True).first()
        if not assignee:
            raise HTTPException(status_code=404, detail="Сотрудник не найден")
        if task.role not in user_roles(assignee) and not user_has_role(assignee, "admin", "manager"):
            raise HTTPException(status_code=400, detail="Роль сотрудника не совпадает с ролью задачи")

    task.assigned_user_id = assignee.id if assignee else None
    task.status = "assigned"
    task.started_at = None
    db.commit()
    return _task_payload(task, db)


@router.post("/{task_id}/deadline")
def set_task_deadline(
    task_id: int,
    payload: TaskDeadlinePayload,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin", "manager")),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.status == "done":
        raise HTTPException(status_code=400, detail="Задача уже закрыта")

    if payload.due_date:
        try:
            task.due_date = datetime.fromisoformat(payload.due_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Некорректная дата дедлайна")
    else:
        task.due_date = None

    db.commit()
    return _task_payload(task, db)


@router.post("/{task_id}/hold")
def hold_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.query(WorkflowTask).filter(WorkflowTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if not _can_access_task(task, user):
        raise HTTPException(status_code=403, detail="Эта задача назначена другой роли")
    if task.status == "done":
        raise HTTPException(status_code=400, detail="Закрытую задачу нельзя поставить на холд")
    if task.status == "hold":
        return _task_payload(task, db)

    payload = task.payload or {}
    task.payload = {**payload, "previous_status": task.status}
    task.status = "hold"
    db.commit()
    return _task_payload(task, db)


@router.post("/{task_id}/resume")
def resume_task(
    task_id: int,
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

    payload = task.payload or {}
    previous_status = payload.get("previous_status") or ("in_progress" if task.assigned_user_id else "assigned")
    if previous_status not in ["assigned", "in_progress", "open", "waiting_delivery"]:
        previous_status = "assigned"
    task.status = previous_status
    task.payload = {key: value for key, value in payload.items() if key != "previous_status"}
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

    task_payload = task.payload or {}
    notes = task_payload.get("notes", [])
    notes.append({
        "author": user.username,
        "role": user.role,
        "roles": user_roles(user),
        "text": payload.note,
    })
    task.payload = {**task_payload, "notes": notes}
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
    allowed_statuses = ["in_progress", "open"]
    if task.type == "assembler_build":
        allowed_statuses.append("assigned")
    if task.status not in allowed_statuses or not _can_work_task(task, user):
        raise HTTPException(status_code=400, detail="Сначала задачу нужно взять в работу")

    result = complete_task(db, task, payload.payload, actor_user_id=user.id)
    db.commit()
    return {"status": "success", "task_id": task.id, "result": result}
