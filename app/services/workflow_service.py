from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.inventory import Stock
from app.models.production import Order, WorkflowTask


def create_task(db: Session, *, order_id: int, task_type: str, title: str, role: str,
                description: str = "", payload: dict | None = None) -> WorkflowTask:
    task = WorkflowTask(
        order_id=order_id,
        type=task_type,
        title=title,
        description=description,
        role=role,
        status="open",
        payload=payload or {},
    )
    db.add(task)
    db.flush()
    return task


def find_shortages(db: Session, materials: list[dict]) -> list[dict]:
    shortages = []
    for material in materials:
        stock = db.query(Stock).filter(Stock.component_id == material["component_id"]).first()
        actual_qty = stock.actual_qty if stock and stock.actual_qty else 0
        reserved_qty = stock.reserved_qty if stock and stock.reserved_qty else 0
        available = actual_qty - reserved_qty
        shortage_qty = material["qty"] - available
        if shortage_qty > 0:
            shortages.append({
                "component_id": material["component_id"],
                "required_qty": material["qty"],
                "available_qty": available,
                "shortage_qty": shortage_qty,
            })
    return shortages


def create_initial_order_tasks(db: Session, order: Order, materials: list[dict], shortages: list[dict]):
    if shortages:
        order.status = "Procurement Required"
        create_task(
            db,
            order_id=order.id,
            task_type="procurement_purchase",
            title=f"Закупить недостающие компоненты для заказа #{order.id}",
            role="procurement",
            description="Загрузить счет, указать ожидаемую дату поступления и закрыть задачу после оформления закупки.",
            payload={"shortages": shortages},
        )
        return

    order.status = "Reserved"
    create_task(
        db,
        order_id=order.id,
        task_type="warehouse_issue_materials",
        title=f"Выдать комплектующие по заказу #{order.id}",
        role="warehouse",
        description="Передать зарезервированные комплектующие сборщику.",
        payload={"materials": materials},
    )


def complete_task(db: Session, task: WorkflowTask, completion_payload: dict | None = None):
    if task.status == "done":
        raise HTTPException(status_code=400, detail="Задача уже закрыта")

    payload = task.payload or {}
    completion_payload = completion_payload or {}
    task.payload = {**payload, "completion": completion_payload}
    task.status = "done"
    task.completed_at = datetime.utcnow()

    order = db.query(Order).filter(Order.id == task.order_id).first() if task.order_id else None

    if task.type == "procurement_purchase":
        if order:
            order.status = "Awaiting Components"
        create_task(
            db,
            order_id=task.order_id,
            task_type="warehouse_receive_components",
            title=f"Принять закупленные комплектующие по заказу #{task.order_id}",
            role="warehouse",
            description="После поступления принять компоненты на склад и подтвердить готовность к выдаче.",
            payload={
                "shortages": payload.get("shortages", []),
                "invoice": completion_payload.get("invoice"),
                "expected_date": completion_payload.get("expected_date"),
            },
        )

    elif task.type == "warehouse_receive_components":
        if order:
            order.status = "Components Available"
        create_task(
            db,
            order_id=task.order_id,
            task_type="warehouse_issue_materials",
            title=f"Выдать комплектующие по заказу #{task.order_id}",
            role="warehouse",
            description="Передать комплектующие сборщику.",
        )

    elif task.type == "warehouse_issue_materials":
        if order:
            order.status = "Materials Issued"
        create_task(
            db,
            order_id=task.order_id,
            task_type="assembler_receive_materials",
            title=f"Получить комплектующие по заказу #{task.order_id}",
            role="assembler",
            description="Подтвердить получение комплекта в сборку.",
        )

    elif task.type == "assembler_receive_materials":
        if order:
            order.status = "In Assembly"
        create_task(
            db,
            order_id=task.order_id,
            task_type="assembler_build",
            title=f"Собрать изделия по заказу #{task.order_id}",
            role="assembler",
            description="Отметить количество собранных изделий.",
        )

    elif task.type == "assembler_build":
        if order:
            order.status = "Quality Check"
        create_task(
            db,
            order_id=task.order_id,
            task_type="tester_check",
            title=f"Протестировать изделия по заказу #{task.order_id}",
            role="tester",
            description="Отметить годные и бракованные изделия.",
            payload={"assembled_qty": completion_payload.get("assembled_qty")},
        )

    elif task.type == "tester_check":
        defective_qty = int(completion_payload.get("defective_qty") or 0)
        if defective_qty > 0:
            if order:
                order.status = "Repair Required"
            create_task(
                db,
                order_id=task.order_id,
                task_type="repair_defects",
                title=f"Устранить брак по заказу #{task.order_id}",
                role="repair_engineer",
                description="Устранить выявленные дефекты и передать изделия на упаковку.",
                payload={"defective_qty": defective_qty, "notes": completion_payload.get("notes")},
            )
        else:
            if order:
                order.status = "Ready For Packing"
            create_task(
                db,
                order_id=task.order_id,
                task_type="packer_pack",
                title=f"Упаковать изделия по заказу #{task.order_id}",
                role="packer",
                description="Упаковать годные изделия и передать на склад готовой продукции.",
            )

    elif task.type == "repair_defects":
        if order:
            order.status = "Ready For Packing"
        create_task(
            db,
            order_id=task.order_id,
            task_type="packer_pack",
            title=f"Упаковать изделия по заказу #{task.order_id}",
            role="packer",
            description="Упаковать исправленные изделия и передать на склад готовой продукции.",
        )

    elif task.type == "packer_pack":
        if order:
            order.status = "Finished Goods"
        create_task(
            db,
            order_id=task.order_id,
            task_type="warehouse_finished_goods",
            title=f"Оприходовать готовую продукцию по заказу #{task.order_id}",
            role="warehouse",
            description="Поставить готовые изделия на баланс склада готовой продукции.",
            payload={"packed_qty": completion_payload.get("packed_qty")},
        )

    elif task.type == "warehouse_finished_goods":
        if order:
            order.status = "Ready To Ship"
