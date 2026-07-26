import re
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.auth import User
from app.time_utils import utcnow
from app.models.production import (
    TaskDependency,
    TaskEvent,
    TaskNotification,
    TaskWatcher,
    WorkflowTask,
)


TASK_PRIORITIES = {"low", "normal", "high", "critical"}
DEPENDENCY_TYPES = {"blocks", "relates_to", "duplicates"}
FINAL_STATUSES = {"done", "cancelled"}
SYSTEM_STATUSES = {"waiting_delivery", "ready_to_issue"}
ACTIVE_STATUSES = {"open", "assigned", "in_progress", "hold", *SYSTEM_STATUSES}

TRANSITIONS = {
    "open": {"assigned", "in_progress", "hold", "cancelled"},
    "assigned": {"open", "in_progress", "hold", "cancelled"},
    "in_progress": {"hold", "done", "cancelled"},
    "hold": {"open", "assigned", "in_progress", "cancelled"},
    "waiting_delivery": {"ready_to_issue", "hold", "cancelled"},
    "ready_to_issue": {"in_progress", "hold", "done", "cancelled"},
    "done": {"open"},
    "cancelled": {"open"},
}

SLA_HOURS = {
    "low": 168,
    "normal": 72,
    "high": 24,
    "critical": 4,
}

MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-zА-Яа-я0-9_.-]+)")

def calculate_sla_due(priority: str, start_at: datetime | None = None) -> datetime:
    if priority not in TASK_PRIORITIES:
        raise HTTPException(status_code=422, detail="Некорректный приоритет")
    return (start_at or utcnow()) + timedelta(hours=SLA_HOURS[priority])


def blocking_dependencies(db: Session, task_id: int) -> list[WorkflowTask]:
    return (
        db.query(WorkflowTask)
        .join(TaskDependency, TaskDependency.depends_on_task_id == WorkflowTask.id)
        .filter(
            TaskDependency.task_id == task_id,
            TaskDependency.dependency_type == "blocks",
            WorkflowTask.status.notin_(FINAL_STATUSES),
        )
        .all()
    )


def ensure_not_blocked(db: Session, task: WorkflowTask):
    blockers = blocking_dependencies(db, task.id)
    if blockers:
        labels = ", ".join(f"#{item.id} {item.title}" for item in blockers[:5])
        raise HTTPException(status_code=409, detail=f"Задача заблокирована: {labels}")


def _notification_recipients(db: Session, task: WorkflowTask, actor_user_id: int | None) -> set[int]:
    recipients = {
        watcher.user_id
        for watcher in db.query(TaskWatcher).filter(TaskWatcher.task_id == task.id).all()
    }
    if task.assigned_user_id:
        recipients.add(task.assigned_user_id)
    if task.created_by_user_id:
        recipients.add(task.created_by_user_id)
    if actor_user_id:
        recipients.discard(actor_user_id)
    return recipients


def record_event(
    db: Session,
    task: WorkflowTask,
    event_type: str,
    actor_user_id: int | None,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    reason: str | None = None,
    data: dict | None = None,
) -> TaskEvent:
    event = TaskEvent(
        task_id=task.id,
        actor_user_id=actor_user_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        data=data or {},
    )
    db.add(event)
    db.flush()
    for user_id in _notification_recipients(db, task, actor_user_id):
        db.add(TaskNotification(event_id=event.id, task_id=task.id, user_id=user_id))
    return event


def add_watcher(db: Session, task: WorkflowTask, user_id: int, actor_user_id: int | None):
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=404, detail="Наблюдатель не найден")
    watcher = db.query(TaskWatcher).filter_by(task_id=task.id, user_id=user_id).first()
    if watcher:
        return watcher
    watcher = TaskWatcher(task_id=task.id, user_id=user_id, added_by_user_id=actor_user_id)
    db.add(watcher)
    db.flush()
    record_event(
        db, task, "watcher_added", actor_user_id,
        data={"user_id": user_id},
    )
    return watcher


def add_mentioned_watchers(db: Session, task: WorkflowTask, text: str, actor_user_id: int | None) -> list[int]:
    usernames = {match.group(1) for match in MENTION_RE.finditer(text or "")}
    if not usernames:
        return []
    users = db.query(User).filter(User.username.in_(usernames), User.is_active == True).all()
    for user in users:
        add_watcher(db, task, user.id, actor_user_id)
    return [user.id for user in users]


def transition_task(
    db: Session,
    task: WorkflowTask,
    target_status: str,
    actor: User,
    *,
    reason: str | None = None,
    allow_system: bool = False,
):
    current = task.status
    if target_status == current:
        return task
    if target_status in SYSTEM_STATUSES and not allow_system:
        raise HTTPException(status_code=403, detail="Этот статус изменяется системой")
    if target_status not in TRANSITIONS.get(current, set()):
        raise HTTPException(status_code=409, detail=f"Переход {current} → {target_status} запрещён")
    if (target_status in {"hold", "cancelled"} or current == "hold") and not (reason or "").strip():
        raise HTTPException(status_code=422, detail="Укажите причину изменения статуса")
    if current in FINAL_STATUSES:
        from app.services.auth_service import user_has_role
        if not user_has_role(actor, "admin", "manager"):
            raise HTTPException(status_code=403, detail="Изменять закрытые задачи может только менеджер")
        if not (reason or "").strip():
            raise HTTPException(status_code=422, detail="Для возврата закрытой задачи нужна причина")
    if target_status in {"in_progress", "done"}:
        ensure_not_blocked(db, task)

    now = utcnow()
    task.status = target_status
    if target_status == "in_progress":
        task.started_at = task.started_at or now
    if target_status == "done":
        task.completed_at = now
    elif current == "done":
        task.completed_at = None
    if target_status == "cancelled":
        task.cancelled_at = now
        task.cancel_reason = reason.strip()
    elif current == "cancelled":
        task.cancelled_at = None
        task.cancel_reason = None
    if target_status == "hold":
        task.hold_reason = reason.strip()
    elif current == "hold":
        task.hold_reason = None

    record_event(
        db,
        task,
        "status_changed",
        actor.id,
        from_status=current,
        to_status=target_status,
        reason=(reason or "").strip() or None,
    )
    return task


def task_runtime_payload(db: Session, task: WorkflowTask) -> dict:
    now = utcnow()
    deadline = task.due_date or task.sla_due_at
    overdue = bool(deadline and task.status not in FINAL_STATUSES and deadline < now)
    blockers = blocking_dependencies(db, task.id)
    return {
        "is_overdue": overdue,
        "effective_deadline": deadline,
        "sla_status": (
            "completed" if task.status == "done"
            else "breached" if overdue
            else "at_risk" if deadline and deadline - now <= timedelta(hours=4)
            else "on_track"
        ),
        "blocked": bool(blockers),
        "blockers": [{"id": item.id, "title": item.title, "status": item.status} for item in blockers],
    }


def ensure_dependency_is_valid(db: Session, task_id: int, depends_on_task_id: int, dependency_type: str):
    if dependency_type not in DEPENDENCY_TYPES:
        raise HTTPException(status_code=422, detail="Некорректный тип зависимости")
    if task_id == depends_on_task_id:
        raise HTTPException(status_code=422, detail="Задача не может зависеть от самой себя")
    if not db.get(WorkflowTask, depends_on_task_id):
        raise HTTPException(status_code=404, detail="Связанная задача не найдена")
    if dependency_type != "blocks":
        return

    visited = {task_id}
    frontier = [depends_on_task_id]
    while frontier:
        current = frontier.pop()
        if current in visited:
            raise HTTPException(status_code=409, detail="Зависимость создаёт цикл")
        visited.add(current)
        frontier.extend(
            row.depends_on_task_id
            for row in db.query(TaskDependency).filter_by(task_id=current, dependency_type="blocks").all()
        )
