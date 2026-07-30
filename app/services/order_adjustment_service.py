from __future__ import annotations

from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.inventory import Stock
from app.models.production import Item, MaterialTransfer, Order, OrderItem, Reservation, WorkflowTask
from app.services.production_planning import get_bom_requirements
from app.services.workflow_service import (
    create_procurement_task_for_order,
    create_task,
    enrich_component_lines,
    ensure_assembly_device_pool,
    find_order_shortages,
    reconcile_stock_reservations,
)
from app.time_utils import utcnow


ACTIVE_TASK_STATUSES = {"assigned", "open", "in_progress", "waiting_delivery", "hold", "ready_to_issue"}
LOCKED_UNIT_STATUSES = {"assembly", "assembled", "testing", "passed", "repair", "packed"}


def _issued_by_component(db: Session, order_id: int) -> dict[int, float]:
    issued: dict[int, float] = defaultdict(float)
    transfers = db.query(MaterialTransfer).filter(
        MaterialTransfer.order_id == order_id,
        MaterialTransfer.status.in_(["issued", "accepted"]),
    ).all()
    for transfer in transfers:
        for line in transfer.lines:
            issued[int(line.component_id)] += float(line.issued_qty or line.accepted_qty or 0)
    return dict(issued)


def _requirements_for_quantities(db: Session, order: Order, quantities: dict[int, int]) -> dict[int, float]:
    result: dict[int, float] = defaultdict(float)
    for order_item in order.items:
        quantity = int(quantities.get(order_item.id, order_item.quantity) or 0)
        for line in get_bom_requirements(order_item.product_id, quantity, db):
            result[int(line["component_id"])] += float(line.get("qty") or 0)
    return dict(result)


def _unit_snapshot(db: Session, order_item: OrderItem, new_quantity: int) -> dict:
    units = db.query(Item).filter(
        Item.order_item_id == order_item.id,
        Item.is_order_surplus.is_(False),
    ).order_by(Item.id.asc()).all()
    locked = [
        unit for unit in units
        if unit.status in LOCKED_UNIT_STATUSES
        or (unit.status == "planned" and unit.assigned_user_id is not None)
    ]
    stocked = [unit for unit in units if unit.status == "stocked"]
    removable_planned = [
        unit for unit in units
        if unit.status == "planned" and unit.assigned_user_id is None
    ]
    minimum_quantity = len(locked)
    stocked_for_order = max(min(len(stocked), new_quantity - minimum_quantity), 0)
    return {
        "locked_units": locked,
        "stocked_units": stocked,
        "removable_planned_units": removable_planned,
        "minimum_quantity": minimum_quantity,
        "surplus_stocked_qty": max(len(stocked) - stocked_for_order, 0),
        "remove_planned_qty": max(
            len(units) - len(stocked) - len(locked) - max(new_quantity - len(stocked) - len(locked), 0),
            0,
        ),
    }


def preview_order_adjustment(db: Session, order: Order, quantities: dict[int, int]) -> dict:
    unknown_ids = set(quantities) - {item.id for item in order.items}
    if unknown_ids:
        raise HTTPException(status_code=404, detail=f"Позиции заказа не найдены: {sorted(unknown_ids)}")

    lines = []
    for order_item in order.items:
        new_quantity = int(quantities.get(order_item.id, order_item.quantity))
        if new_quantity <= 0:
            raise HTTPException(status_code=422, detail="Количество изделия должно быть больше нуля")
        snapshot = _unit_snapshot(db, order_item, new_quantity)
        if new_quantity < snapshot["minimum_quantity"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Нельзя уменьшить {order_item.product.name if order_item.product else 'изделие'} "
                    f"ниже {snapshot['minimum_quantity']} шт.: столько устройств уже находится в производстве"
                ),
            )
        lines.append({
            "order_item_id": order_item.id,
            "product_id": order_item.product_id,
            "product_name": order_item.product.name if order_item.product else f"Изделие ID {order_item.product_id}",
            "old_quantity": int(order_item.quantity or 0),
            "new_quantity": new_quantity,
            "delta": new_quantity - int(order_item.quantity or 0),
            "locked_wip_qty": snapshot["minimum_quantity"],
            "stocked_qty": len(snapshot["stocked_units"]),
            "surplus_to_free_stock": snapshot["surplus_stocked_qty"],
            "planned_units_to_remove": min(snapshot["remove_planned_qty"], len(snapshot["removable_planned_units"])),
        })

    old_quantities = {item.id: int(item.quantity or 0) for item in order.items}
    old_requirements = _requirements_for_quantities(db, order, old_quantities)
    new_requirements = _requirements_for_quantities(db, order, quantities)
    issued = _issued_by_component(db, order.id)
    return_materials = []
    for component_id, issued_qty in issued.items():
        return_qty = max(issued_qty - float(new_requirements.get(component_id, 0)), 0)
        if return_qty > 0:
            return_materials.append({"component_id": component_id, "qty": return_qty})

    return {
        "order_id": order.id,
        "lines": lines,
        "return_materials": enrich_component_lines(db, return_materials),
        "return_materials_total": sum(line["qty"] for line in return_materials),
        "requirements_delta": {
            component_id: float(new_requirements.get(component_id, 0)) - float(old_requirements.get(component_id, 0))
            for component_id in set(old_requirements) | set(new_requirements)
            if float(new_requirements.get(component_id, 0)) != float(old_requirements.get(component_id, 0))
        },
    }


def _resize_task_payload(task: WorkflowTask, quantity_by_item: dict[int, int]) -> None:
    payload = dict(task.payload or {})
    context = dict(payload.get("product_context") or {})
    order_item_id = context.get("order_item_id")
    if order_item_id and int(order_item_id) in quantity_by_item:
        new_quantity = quantity_by_item[int(order_item_id)]
        context["qty"] = new_quantity
        payload["product_context"] = context
        if payload.get("planned_qty") is not None:
            payload["planned_qty"] = new_quantity

    if isinstance(payload.get("product_lines"), list):
        payload["product_lines"] = [
            {
                **line,
                "qty": quantity_by_item.get(int(line["order_item_id"]), line.get("qty"))
                if line.get("order_item_id") else line.get("qty"),
            }
            for line in payload["product_lines"]
        ]
    task.payload = payload


def _reconcile_reservations(db: Session, order: Order) -> None:
    issued = _issued_by_component(db, order.id)
    requirements = _requirements_for_quantities(
        db,
        order,
        {item.id: int(item.quantity or 0) for item in order.items},
    )
    desired = {
        component_id: max(required_qty - float(issued.get(component_id, 0)), 0)
        for component_id, required_qty in requirements.items()
    }
    reservations_by_component: dict[int, list[Reservation]] = defaultdict(list)
    for reservation in db.query(Reservation).filter(Reservation.order_id == order.id).all():
        reservations_by_component[int(reservation.component_id)].append(reservation)

    for component_id in set(desired) | set(reservations_by_component):
        rows = reservations_by_component.get(component_id, [])
        current = sum(float(row.qty or 0) for row in rows)
        target = float(desired.get(component_id, 0))
        stock = db.query(Stock).filter(Stock.component_id == component_id).with_for_update().first()
        if target < current:
            release = current - target
            if stock:
                stock.reserved_qty = max(float(stock.reserved_qty or 0) - release, 0)
            remaining = target
            for row in rows:
                next_qty = min(float(row.qty or 0), remaining)
                row.qty = next_qty
                remaining -= next_qty
                if row.qty <= 0:
                    db.delete(row)
        elif target > current and stock:
            available = max(float(stock.actual_qty or 0) - float(stock.reserved_qty or 0), 0)
            additional = min(target - current, available)
            if additional > 0:
                stock.reserved_qty = float(stock.reserved_qty or 0) + additional
                if rows:
                    rows[0].qty = float(rows[0].qty or 0) + additional
                else:
                    db.add(Reservation(order_id=order.id, component_id=component_id, qty=additional))


def apply_order_adjustment(
    db: Session,
    order: Order,
    quantities: dict[int, int],
    *,
    reason: str,
    actor_user_id: int | None,
) -> dict:
    preview = preview_order_adjustment(db, order, quantities)
    changed_lines = [line for line in preview["lines"] if line["delta"]]
    if not changed_lines:
        raise HTTPException(status_code=400, detail="Количество изделий не изменилось")

    quantity_by_item = {line["order_item_id"]: line["new_quantity"] for line in preview["lines"]}
    for order_item in order.items:
        order_item.quantity = quantity_by_item[order_item.id]
        snapshot = _unit_snapshot(db, order_item, order_item.quantity)
        surplus_qty = snapshot["surplus_stocked_qty"]
        for unit in snapshot["stocked_units"][-surplus_qty:] if surplus_qty else []:
            unit.is_order_surplus = True
        remove_qty = min(snapshot["remove_planned_qty"], len(snapshot["removable_planned_units"]))
        for unit in snapshot["removable_planned_units"][-remove_qty:] if remove_qty else []:
            db.delete(unit)

    active_tasks = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order.id,
        WorkflowTask.status.in_(ACTIVE_TASK_STATUSES),
    ).all()
    for task in active_tasks:
        _resize_task_payload(task, quantity_by_item)
    db.flush()
    for task in active_tasks:
        if task.type == "assembler_build":
            ensure_assembly_device_pool(db, task)

    _reconcile_reservations(db, order)
    reconcile_stock_reservations(db)

    shortages = find_order_shortages(db, order.id)
    procurement = next((
        task for task in active_tasks if task.type == "procurement_purchase"
    ), None)
    if shortages:
        if procurement:
            procurement.payload = {**(procurement.payload or {}), "shortages": shortages}
            procurement.status = "in_progress"
            procurement.completed_at = None
        else:
            create_procurement_task_for_order(db, order, shortages)
    elif procurement and not (procurement.payload or {}).get("purchases"):
        procurement.status = "cancelled"
        procurement.cancelled_at = utcnow()
        procurement.payload = {**(procurement.payload or {}), "cancel_reason": "Потребность снята изменением количества заказа"}

    if preview["return_materials"]:
        existing_return = db.query(WorkflowTask).filter(
            WorkflowTask.order_id == order.id,
            WorkflowTask.type == "order_adjustment_return",
            WorkflowTask.status.in_(ACTIVE_TASK_STATUSES),
        ).first()
        return_payload = {
            "materials": preview["return_materials"],
            "items": preview["return_materials"],
            "adjustment_reason": reason,
        }
        if existing_return:
            existing_return.payload = return_payload
        else:
            create_task(
                db,
                order_id=order.id,
                task_type="order_adjustment_return",
                title=f"Вернуть лишние комплектующие по заказу #{order.id}",
                role="warehouse",
                description="Принять обратно неиспользованные комплектующие после уменьшения количества изделий.",
                payload=return_payload,
            )

    event = {
        "created_at": utcnow().isoformat(),
        "actor_user_id": actor_user_id,
        "reason": reason,
        "lines": changed_lines,
        "return_materials": preview["return_materials"],
    }
    order.adjustment_history = [*(order.adjustment_history or []), event]
    db.flush()
    return {**preview, "status": "applied", "event": event}
