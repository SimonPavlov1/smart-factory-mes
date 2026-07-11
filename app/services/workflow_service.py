import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.inventory import Component, Stock
from app.models.production import Order, OrderItem, Reservation, WorkflowTask
from app.services.production_planning import get_bom_requirements
from app.services.reservation_service import reserve_components


def create_task(db: Session, *, order_id: int, task_type: str, title: str, role: str,
                description: str = "", payload: dict | None = None) -> WorkflowTask:
    task = WorkflowTask(
        order_id=order_id,
        type=task_type,
        title=title,
        description=description,
        role=role,
        status="assigned",
        payload=payload or {},
    )
    db.add(task)
    db.flush()
    return task


def _component_label(component: Component | None, component_id: int) -> dict:
    if not component:
        return {
            "component_id": component_id,
            "component_name": f"Компонент ID {component_id}",
            "part_number": None,
            "category": None,
        }
    return {
        "component_id": component.id,
        "component_name": component.name,
        "part_number": component.part_number,
        "category": component.category,
        "package": component.package,
        "value": component.value,
    }


def enrich_component_lines(db: Session, lines: list[dict]) -> list[dict]:
    component_ids = [line["component_id"] for line in lines if line.get("component_id")]
    components = {
        component.id: component
        for component in db.query(Component).filter(Component.id.in_(component_ids)).all()
    } if component_ids else {}
    return [
        {**_component_label(components.get(line["component_id"]), line["component_id"]), **line}
        for line in lines
    ]


def find_shortages(db: Session, materials: list[dict]) -> list[dict]:
    shortages = []
    components = {
        component.id: component
        for component in db.query(Component).filter(Component.id.in_([m["component_id"] for m in materials])).all()
    } if materials else {}
    for material in materials:
        stock = db.query(Stock).filter(Stock.component_id == material["component_id"]).first()
        actual_qty = stock.actual_qty if stock and stock.actual_qty else 0
        reserved_qty = stock.reserved_qty if stock and stock.reserved_qty else 0
        available = actual_qty - reserved_qty
        shortage_qty = material["qty"] - available
        if shortage_qty > 0:
            shortages.append({
                **_component_label(components.get(material["component_id"]), material["component_id"]),
                "component_id": material["component_id"],
                "required_qty": material["qty"],
                "available_qty": available,
                "shortage_qty": shortage_qty,
            })
    return shortages


def _line_qty_map(items: list[dict]) -> dict[int, float]:
    result = {}
    for item in items or []:
        component_id = item.get("component_id")
        if not component_id:
            continue
        qty = float(item.get("qty") or 0)
        if qty > 0:
            result[int(component_id)] = result.get(int(component_id), 0) + qty
    return result


def _delivery_lines(completion_payload: dict) -> list[dict]:
    deliveries = completion_payload.get("deliveries") or []
    if deliveries:
        result = []
        for delivery in deliveries:
            component_id = delivery.get("component_id")
            qty = float(delivery.get("qty") or 0)
            if not component_id or qty <= 0:
                continue
            result.append({
                "component_id": int(component_id),
                "qty": qty,
                "expected_date": delivery.get("expected_date") or completion_payload.get("expected_date"),
                "invoice": delivery.get("invoice") or completion_payload.get("invoice"),
                "supplier": delivery.get("supplier"),
                "comment": delivery.get("comment"),
            })
        return result

    return [
        {
            "component_id": int(component_id),
            "qty": qty,
            "expected_date": completion_payload.get("expected_date"),
            "invoice": completion_payload.get("invoice"),
            "supplier": completion_payload.get("supplier"),
            "comment": completion_payload.get("comment"),
        }
        for component_id, qty in _line_qty_map(completion_payload.get("items", [])).items()
    ]


def _delivery_qty_map(deliveries: list[dict]) -> dict[int, float]:
    result = {}
    for delivery in deliveries:
        component_id = int(delivery["component_id"])
        result[component_id] = result.get(component_id, 0) + float(delivery.get("qty") or 0)
    return result


def _split_deliveries_by_remaining(lines: list[dict], remaining_qty: dict[int, float]) -> list[dict]:
    result = []
    for line in lines:
        component_id = int(line["component_id"])
        allowed = float(remaining_qty.get(component_id, 0))
        if allowed <= 0:
            continue
        qty = min(float(line.get("qty") or 0), allowed)
        if qty <= 0:
            continue
        result.append({**line, "qty": qty})
        remaining_qty[component_id] = allowed - qty
    return result


def _group_deliveries(deliveries: list[dict]) -> list[dict]:
    groups = {}
    for delivery in deliveries:
        key = (
            delivery.get("expected_date") or "",
            delivery.get("invoice") or "",
            delivery.get("supplier") or "",
            delivery.get("comment") or "",
        )
        if key not in groups:
            groups[key] = {
                "expected_date": delivery.get("expected_date"),
                "invoice": delivery.get("invoice"),
                "supplier": delivery.get("supplier"),
                "comment": delivery.get("comment"),
                "items": [],
            }
        groups[key]["items"].append(delivery)
    return list(groups.values())


def _split_component_lines(lines: list[dict], accepted_qty: dict[int, float]) -> tuple[list[dict], list[dict]]:
    accepted = []
    remaining = []
    for line in lines:
        component_id = int(line["component_id"])
        source_qty = float(line.get("shortage_qty") or line.get("qty") or 0)
        qty = min(float(accepted_qty.get(component_id, 0)), source_qty)
        if qty > 0:
            accepted.append({**line, "qty": qty, "shortage_qty": qty})
        if source_qty - qty > 0:
            remaining.append({**line, "shortage_qty": source_qty - qty, "qty": source_qty - qty})
    return accepted, remaining


def _receive_components_to_stock(db: Session, lines: list[dict]):
    for line in lines:
        component_id = int(line["component_id"])
        qty = float(line.get("qty") or line.get("shortage_qty") or 0)
        if qty <= 0:
            continue
        stock = db.query(Stock).filter(Stock.component_id == component_id).with_for_update().first()
        if stock:
            stock.actual_qty = (stock.actual_qty or 0) + qty
        else:
            db.add(Stock(component_id=component_id, actual_qty=qty, location="Warehouse-1"))


def _update_procurement_purchase_receipt(db: Session, payload: dict, received: list[dict]):
    procurement_task_id = payload.get("procurement_task_id")
    purchase_id = payload.get("purchase_id")
    if not procurement_task_id or not purchase_id:
        return

    procurement_task = db.query(WorkflowTask).filter(WorkflowTask.id == procurement_task_id).first()
    if not procurement_task:
        return

    procurement_payload = procurement_task.payload or {}
    received_qty = sum(float(line.get("qty") or line.get("shortage_qty") or 0) for line in received)
    purchases = []
    for purchase in procurement_payload.get("purchases", []):
        if purchase.get("id") == purchase_id:
            purchase = {
                **purchase,
                "received_qty": float(purchase.get("received_qty") or 0) + received_qty,
            }
        purchases.append(purchase)
    procurement_task.payload = {**procurement_payload, "purchases": purchases}


def _open_task_exists(db: Session, order_id: int, task_type: str) -> bool:
    return db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order_id,
        WorkflowTask.type == task_type,
        WorkflowTask.status.in_(["assigned", "in_progress", "open", "waiting_delivery"]),
    ).first() is not None


def _calculate_order_materials(db: Session, order_id: int) -> list[dict]:
    total = {}
    order_items = db.query(OrderItem).filter(OrderItem.order_id == order_id).all()
    for item in order_items:
        for material in get_bom_requirements(item.product_id, item.quantity, db):
            component_id = material["component_id"]
            if component_id in total:
                total[component_id]["qty"] += material["qty"]
            else:
                total[component_id] = {"component_id": component_id, "qty": material["qty"]}
    return list(total.values())


def _ensure_order_reserved(db: Session, order_id: int):
    existing = db.query(Reservation).filter(Reservation.order_id == order_id).first()
    if existing:
        return

    materials = _calculate_order_materials(db, order_id)
    shortages = find_shortages(db, materials)
    if shortages:
        raise HTTPException(status_code=400, detail={"message": "Комплектующие все еще в дефиците", "shortages": shortages})

    reserve_components(db, materials)
    for material in materials:
        db.add(Reservation(order_id=order_id, component_id=material["component_id"], qty=material["qty"]))


def _issue_reserved_materials(db: Session, order_id: int):
    reservations = db.query(Reservation).filter(Reservation.order_id == order_id).all()
    if not reservations:
        raise HTTPException(status_code=400, detail="По заказу нет резерва материалов")

    for item in reservations:
        stock = db.query(Stock).filter(Stock.component_id == item.component_id).with_for_update().first()
        if not stock:
            raise HTTPException(status_code=404, detail=f"Компонент {item.component_id} отсутствует на складе")
        stock.actual_qty = stock.actual_qty or 0
        stock.reserved_qty = stock.reserved_qty or 0
        if stock.actual_qty < item.qty or stock.reserved_qty < item.qty:
            raise HTTPException(status_code=400, detail=f"Недостаточно резерва компонента ID {item.component_id}")
        stock.actual_qty -= item.qty
        stock.reserved_qty -= item.qty


def _receive_finished_goods(db: Session, order_id: int):
    order_items = db.query(OrderItem).filter(OrderItem.order_id == order_id).all()
    for item in order_items:
        stock = db.query(Stock).filter(Stock.product_id == item.product_id).first()
        if stock:
            stock.actual_qty = (stock.actual_qty or 0) + item.quantity
        else:
            db.add(Stock(product_id=item.product_id, actual_qty=item.quantity, location="Finished Goods"))


def _complete_procurement_task(db: Session, task: WorkflowTask, completion_payload: dict, order: Order | None):
    payload = task.payload or {}
    shortages = payload.get("shortages", [])
    deliveries = _delivery_lines(completion_payload)
    purchased, remaining = _split_component_lines(shortages, _delivery_qty_map(deliveries))
    accepted_by_component = {line["component_id"]: line["qty"] for line in purchased}
    accepted_deliveries = _split_deliveries_by_remaining(deliveries, accepted_by_component)

    if not accepted_deliveries:
        raise HTTPException(status_code=400, detail="Укажите хотя бы одну закупленную позицию")

    task.payload = {
        **payload,
        "shortages": remaining,
        "last_completion": completion_payload,
    }

    component_lines = {line["component_id"]: line for line in purchased}
    for group in _group_deliveries(accepted_deliveries):
        group_items = [
            {
                **component_lines[delivery["component_id"]],
                "qty": delivery["qty"],
                "shortage_qty": delivery["qty"],
                "expected_date": group.get("expected_date"),
                "invoice": group.get("invoice"),
                "supplier": group.get("supplier"),
                "comment": group.get("comment"),
            }
            for delivery in group["items"]
        ]
        date_label = f" на {group['expected_date']}" if group.get("expected_date") else ""
        create_task(
            db,
            order_id=task.order_id,
            task_type="accounting_payment",
            title=f"Оплатить счет по заказу #{task.order_id}{date_label}",
            role="accounting",
            description="Проверить счет закупщика, оплатить поставку и передать ее в ожидание поступления.",
            payload={
                "shortages": group_items,
                "invoice": group.get("invoice"),
                "expected_date": group.get("expected_date"),
                "supplier": group.get("supplier"),
                "comment": group.get("comment"),
            },
        )

    if remaining:
        if order:
            order.status = "Procurement Required"
        return {"status": "partial", "remaining": remaining}

    task.status = "waiting_delivery"
    task.payload = {**task.payload, "completion": completion_payload}
    if order:
        order.status = "Awaiting Components"
    return {"status": "waiting_delivery"}


def add_procurement_purchase(db: Session, task: WorkflowTask, purchase_payload: dict):
    if task.type != "procurement_purchase":
        raise HTTPException(status_code=400, detail="Закупку можно добавить только в задачу закупщика")
    if task.status == "done":
        raise HTTPException(status_code=400, detail="Задача уже закрыта")

    component_id = purchase_payload.get("component_id")
    qty = float(purchase_payload.get("qty") or 0)
    if not component_id or qty <= 0:
        raise HTTPException(status_code=400, detail="Укажите компонент и количество закупки")

    payload = task.payload or {}
    shortages = payload.get("shortages", [])
    purchased, remaining = _split_component_lines(shortages, {int(component_id): qty})
    if not purchased:
        raise HTTPException(status_code=400, detail="По этой позиции нет остатка к закупке")

    line = purchased[0]
    purchase_id = uuid.uuid4().hex
    purchase = {
        "id": purchase_id,
        "component_id": int(component_id),
        "qty": float(line["qty"]),
        "expected_date": purchase_payload.get("expected_date"),
        "invoice": purchase_payload.get("invoice"),
        "supplier": purchase_payload.get("supplier"),
        "comment": purchase_payload.get("comment"),
        "received_qty": 0,
        "created_at": datetime.utcnow().isoformat(),
    }

    task.payload = {
        **payload,
        "shortages": remaining,
        "purchases": [*(payload.get("purchases") or []), purchase],
    }

    invoice_label = f" {purchase['invoice']}" if purchase.get("invoice") else ""
    create_task(
        db,
        order_id=task.order_id,
        task_type="accounting_payment",
        title=f"Оплатить счет по заказу #{task.order_id}{invoice_label}",
        role="accounting",
        description="Проверить счет закупщика, оплатить поставку и передать ее в ожидание поступления.",
        payload={
            "procurement_task_id": task.id,
            "purchase_id": purchase_id,
            "shortages": [{
                **line,
                "qty": purchase["qty"],
                "shortage_qty": purchase["qty"],
                "expected_date": purchase.get("expected_date"),
                "invoice": purchase.get("invoice"),
                "supplier": purchase.get("supplier"),
                "comment": purchase.get("comment"),
            }],
            "invoice": purchase.get("invoice"),
            "expected_date": purchase.get("expected_date"),
            "supplier": purchase.get("supplier"),
            "comment": purchase.get("comment"),
        },
    )

    if remaining:
        task.status = "in_progress"
        order = db.query(Order).filter(Order.id == task.order_id).first() if task.order_id else None
        if order:
            order.status = "Procurement Required"
        return {"status": "partial", "remaining": remaining, "purchase": purchase}

    task.status = "waiting_delivery"
    order = db.query(Order).filter(Order.id == task.order_id).first() if task.order_id else None
    if order:
        order.status = "Awaiting Components"
    return {"status": "waiting_delivery", "purchase": purchase}


def _complete_warehouse_receive_task(db: Session, task: WorkflowTask, completion_payload: dict, order: Order | None):
    payload = task.payload or {}
    incoming = payload.get("shortages", [])
    received, remaining = _split_component_lines(incoming, _line_qty_map(completion_payload.get("items", [])))

    if not received:
        raise HTTPException(status_code=400, detail="Укажите хотя бы одну принятую позицию")

    _receive_components_to_stock(db, received)
    _update_procurement_purchase_receipt(db, payload, received)
    task.payload = {
        **payload,
        "shortages": remaining,
        "last_completion": completion_payload,
    }

    if remaining:
        if order:
            order.status = "Awaiting Components"
        return {"status": "partial", "remaining": remaining}

    task.status = "done"
    task.completed_at = datetime.utcnow()
    task.payload = {**task.payload, "completion": completion_payload}

    materials = _calculate_order_materials(db, task.order_id)
    shortages = find_shortages(db, materials)
    if shortages:
        if order:
            order.status = "Procurement Required"
        if not _open_task_exists(db, task.order_id, "procurement_purchase"):
            create_task(
                db,
                order_id=task.order_id,
                task_type="procurement_purchase",
                title=f"Дозакупить комплектующие для заказа #{task.order_id}",
                role="procurement",
                description="Закупить оставшиеся позиции, которых все еще не хватает для запуска заказа.",
                payload={"shortages": shortages},
            )
        return {"status": "done", "shortages": shortages}

    _ensure_order_reserved(db, task.order_id)
    if order:
        order.status = "Components Available"
    create_task(
        db,
        order_id=task.order_id,
        task_type="warehouse_issue_materials",
        title=f"Выдать комплектующие по заказу #{task.order_id}",
        role="warehouse",
        description="Передать комплектующие сборщику.",
        payload={"materials": enrich_component_lines(db, materials)},
    )
    return {"status": "done"}


def _complete_accounting_payment_task(db: Session, task: WorkflowTask, completion_payload: dict, order: Order | None):
    payload = task.payload or {}
    date_label = f" на {payload['expected_date']}" if payload.get("expected_date") else ""
    create_task(
        db,
        order_id=task.order_id,
        task_type="warehouse_receive_components",
        title=f"Принять оплаченные комплектующие по заказу #{task.order_id}{date_label}",
        role="warehouse",
        description="Принять на склад фактически поступившие и оплаченные комплектующие.",
        payload={
            **payload,
            "payment": completion_payload,
        },
    )
    if order:
        order.status = "Awaiting Components"
    return {"status": "done"}


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
        payload={"materials": enrich_component_lines(db, materials)},
    )


def complete_task(db: Session, task: WorkflowTask, completion_payload: dict | None = None):
    if task.status == "done":
        raise HTTPException(status_code=400, detail="Задача уже закрыта")

    payload = task.payload or {}
    completion_payload = completion_payload or {}
    order = db.query(Order).filter(Order.id == task.order_id).first() if task.order_id else None

    if task.type == "procurement_purchase":
        return _complete_procurement_task(db, task, completion_payload, order)

    elif task.type == "warehouse_receive_components":
        return _complete_warehouse_receive_task(db, task, completion_payload, order)

    elif task.type == "accounting_payment":
        task.payload = {**payload, "completion": completion_payload}
        task.status = "done"
        task.completed_at = datetime.utcnow()
        return _complete_accounting_payment_task(db, task, completion_payload, order)

    task.payload = {**payload, "completion": completion_payload}
    task.status = "done"
    task.completed_at = datetime.utcnow()

    if task.type == "warehouse_issue_materials":
        _issue_reserved_materials(db, task.order_id)
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
        _receive_finished_goods(db, task.order_id)
        if order:
            order.status = "Ready To Ship"

    return {"status": "done"}
