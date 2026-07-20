import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.auth import User
from app.models.production import WorkflowTask
from app.services.auth_service import get_current_user, require_roles, user_has_role, user_roles
from app.services.workflow_service import add_procurement_purchase, complete_task, enrich_component_lines
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
    payload = task.payload or {}
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
        "created_at": task.created_at,
        "due_date": task.due_date,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
    }


def _can_access_task(task: WorkflowTask, user: User):
    if user_has_role(user, "admin", "manager"):
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
    if task.type not in ["warehouse_issue_materials", "assembler_receive_materials"]:
        raise HTTPException(status_code=400, detail="Для этой задачи форма не предусмотрена")

    materials = enrich_component_lines(db, (task.payload or {}).get("materials") or [])
    is_issue = task.type == "warehouse_issue_materials"
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
    return user_has_role(user, "admin", "manager") or task.assigned_user_id == user.id


def _apply_status_filter(query, status: str):
    if status == "all":
        return query
    if status in ["active", "open"]:
        return query.filter(WorkflowTask.status.in_(["assigned", "in_progress", "open", "waiting_delivery"]))
    return query.filter(WorkflowTask.status == status)


def _safe_filename(filename: str):
    cleaned = re.sub(r"[^A-Za-zА-Яа-я0-9._-]+", "_", filename).strip("._")
    return cleaned or "file"


@router.get("/mine")
def get_my_tasks(
    status: str = "active",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(WorkflowTask)
    if not user_has_role(user, "admin"):
        query = query.filter(
            WorkflowTask.role.in_(user_roles(user)),
            or_(WorkflowTask.assigned_user_id.is_(None), WorkflowTask.assigned_user_id == user.id),
        )
    query = _apply_status_filter(query, status)
    tasks = query.order_by(WorkflowTask.created_at.desc()).all()
    return [_task_payload(task, db) for task in tasks]


@router.get("")
def get_all_tasks(
    status: str = "active",
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin", "manager")),
):
    query = db.query(WorkflowTask)
    query = _apply_status_filter(query, status)
    tasks = query.order_by(WorkflowTask.created_at.desc()).all()
    return [_task_payload(task, db) for task in tasks]


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
    return _task_payload(task, db)


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
    if task.status not in ["in_progress", "open"] or not _can_work_task(task, user):
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
    if task.status not in ["in_progress", "open"] or not _can_work_task(task, user):
        raise HTTPException(status_code=400, detail="Сначала задачу нужно взять в работу")

    result = complete_task(db, task, payload.payload, actor_user_id=user.id)
    db.commit()
    return {"status": "success", "task_id": task.id, "result": result}
