from sqlalchemy.orm import Session

from app.models.production import Order, WorkflowTask


ACTIVE = {"assigned", "open", "in_progress", "waiting_delivery", "hold", "ready_to_issue"}
TERMINAL = {"done", "cancelled", "merged"}


def aggregate_order_progress(db: Session, order: Order) -> dict:
    tasks = db.query(WorkflowTask).filter(WorkflowTask.order_id == order.id).all()
    relevant = [task for task in tasks if task.status not in {"cancelled", "merged"}]
    done = sum(1 for task in relevant if task.status == "done")
    active = [task for task in relevant if task.status in ACTIVE]
    total = len(relevant)
    percent = round((done / total) * 100) if total else 0
    planned_qty = sum(float(item.quantity or 0) for item in order.items)
    finished_tasks = [task for task in tasks if task.type == "warehouse_finished_goods" and task.status == "done"]
    finished_qty = sum(
        sum(float(line.get("qty") or 0) for line in ((task.payload or {}).get("finished_goods") or []))
        for task in finished_tasks
    )
    if total and done == total:
        state = "completed"
        label = "Готов к отгрузке"
        percent = 100
    elif any(task.status == "hold" for task in active):
        state, label = "blocked", "Есть приостановленные задачи"
    elif active:
        state, label = "in_progress", "В работе"
    else:
        state, label = "planned", "Запланирован"
    current = [
        {"id": task.id, "type": task.type, "role": task.role, "status": task.status, "title": task.title}
        for task in sorted(active, key=lambda item: item.id)
    ]
    return {
        "state": state, "label": label, "percent": percent,
        "tasks_done": done, "tasks_total": total, "active_tasks": current,
        "planned_qty": planned_qty, "finished_qty": min(finished_qty, planned_qty),
    }
