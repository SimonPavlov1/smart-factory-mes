import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.inventory import Component, InventoryMovement, Stock
from app.models.auth import User
from app.time_utils import utcnow
from app.models.production import (
    MaterialTransfer,
    MaterialTransferLine,
    Order,
    OrderItem,
    ProductBOM,
    ProductType,
    Item,
    Reservation,
    WorkflowTask,
)
from app.services.production_planning import get_bom_requirements
from app.services.reservation_service import reserve_components
from app.services.inventory_movement_service import record_movement
from app.services.factory_number_service import create_product_units
from app.services.quantity_service import record_completion_batch, sync_task_quantities
from app.services.workflow_routes import validate_task_role, validate_transition
from app.services.workflow_batch_service import (
    ACCUMULATIVE_TYPES,
    batch_summary,
    consume_workflow_batches,
    find_accumulative_task,
    merge_accumulative_payload,
    pending_product_lines,
    record_workflow_batch,
)


def create_task(db: Session, *, order_id: int, task_type: str, title: str, role: str,
                description: str = "", payload: dict | None = None) -> WorkflowTask:
    validate_task_role(task_type, role)
    payload = payload or {}
    accumulator = find_accumulative_task(db, order_id, task_type, payload)
    if accumulator:
        accumulator.title = title
        accumulator.description = description
        accumulator.role = role
        if accumulator.status == "done":
            current = accumulator.payload or {}
            completion = current.get("completion")
            if completion:
                history = list(current.get("completion_history") or [])
                history.append({
                    **completion,
                    "completed_at": accumulator.completed_at.isoformat() if accumulator.completed_at else utcnow().isoformat(),
                })
                accumulator.payload = {**current, "completion": {}, "completion_history": history}
            accumulator.status = "assigned"
            accumulator.completed_at = None
        merge_accumulative_payload(accumulator, payload)
        record_workflow_batch(db, accumulator, payload)
        return accumulator
    task = WorkflowTask(
        order_id=order_id,
        type=task_type,
        title=title,
        description=description,
        role=role,
        status="assigned",
        payload=payload,
    )
    db.add(task)
    db.flush()
    if task_type in ACCUMULATIVE_TYPES:
        record_workflow_batch(db, task, payload)
    return task


def _pending_or_legacy_product_lines(
    db: Session,
    task: WorkflowTask,
    payload_key: str,
    *,
    aggregate_all: bool = False,
) -> list[dict]:
    pending = pending_product_lines(db, task, aggregate_all=aggregate_all)
    if pending:
        return pending
    if batch_summary(db, task)["batches_total"] > 0:
        return []
    return list((task.payload or {}).get(payload_key) or [])


def _material_transfer_payload(transfer: MaterialTransfer) -> dict:
    return {
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


def _ensure_material_transfer(db: Session, issue_task: WorkflowTask, recipient_role: str) -> MaterialTransfer:
    transfer = db.query(MaterialTransfer).filter(MaterialTransfer.issue_task_id == issue_task.id).first()
    if transfer:
        return transfer
    payload = issue_task.payload or {}
    transfer = MaterialTransfer(
        order_id=issue_task.order_id,
        source_task_id=payload.get("source_task_id"),
        issue_task_id=issue_task.id,
        source_task_type=payload.get("source_task_type"),
        recipient_role=recipient_role,
        status="reserved",
    )
    db.add(transfer)
    db.flush()
    for material in payload.get("materials") or []:
        qty = float(material.get("qty") or 0)
        if material.get("component_id") and qty > 0:
            transfer.lines.append(MaterialTransferLine(
                component_id=int(material["component_id"]),
                line_uid=material.get("line_uid"),
                requested_qty=float(material.get("requested_qty") or qty),
                reserved_qty=qty,
                issued_qty=0,
                accepted_qty=0,
            ))
    db.flush()
    issue_task.payload = {**payload, "material_transfer_id": transfer.id}
    return transfer


def _mark_material_transfer_issued(db: Session, issue_task: WorkflowTask, recipient_role: str,
                                   actor_user_id: int | None) -> MaterialTransfer:
    transfer = _ensure_material_transfer(db, issue_task, recipient_role)
    if transfer.status == "accepted":
        raise HTTPException(status_code=400, detail="Передача компонентов уже подтверждена получателем")
    transfer.status = "issued"
    transfer.issued_by_user_id = actor_user_id
    transfer.issued_at = transfer.issued_at or utcnow()
    for line in transfer.lines:
        line.issued_qty = line.reserved_qty
    db.flush()
    return transfer


def _attach_material_transfer_receipt(db: Session, transfer: MaterialTransfer, receive_task: WorkflowTask):
    transfer.receive_task_id = receive_task.id
    receive_task.payload = {
        **(receive_task.payload or {}),
        "material_transfer_id": transfer.id,
        "material_transfer": _material_transfer_payload(transfer),
    }
    issue_task = db.query(WorkflowTask).filter(WorkflowTask.id == transfer.issue_task_id).first()
    if issue_task:
        issue_task.payload = {
            **(issue_task.payload or {}),
            "material_transfer": _material_transfer_payload(transfer),
        }


def _accept_material_transfer(db: Session, receive_task: WorkflowTask, actor_user_id: int | None):
    transfer_id = (receive_task.payload or {}).get("material_transfer_id")
    transfer = (
        db.query(MaterialTransfer).filter(MaterialTransfer.id == int(transfer_id)).first()
        if transfer_id else
        db.query(MaterialTransfer).filter(MaterialTransfer.receive_task_id == receive_task.id).first()
    )
    if not transfer:
        return None
    if transfer.status != "issued":
        raise HTTPException(status_code=400, detail="Складская выдача еще не проведена или передача уже подтверждена")
    transfer.status = "accepted"
    transfer.accepted_by_user_id = actor_user_id
    transfer.accepted_at = utcnow()
    for line in transfer.lines:
        line.accepted_qty = line.issued_qty
    receive_task.payload = {
        **(receive_task.payload or {}),
        "material_transfer": _material_transfer_payload(transfer),
    }
    return transfer


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


def _active_stage_exists(db: Session, order_id: int, task_types: list[str], exclude_task_id: int | None = None) -> bool:
    query = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order_id,
        WorkflowTask.type.in_(task_types),
        WorkflowTask.status.in_(["assigned", "open", "in_progress", "hold", "ready_to_issue"]),
    )
    if exclude_task_id:
        query = query.filter(WorkflowTask.id != exclude_task_id)
    return bool(query.first())


def _active_stage_exists_for_context(
    db: Session,
    order_id: int,
    task_types: list[str],
    product_context: dict | None,
    exclude_task_id: int | None = None,
) -> bool:
    if not product_context:
        return _active_stage_exists(db, order_id, task_types, exclude_task_id)
    query = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order_id,
        WorkflowTask.type.in_(task_types),
        WorkflowTask.status.in_(["assigned", "open", "in_progress", "hold", "ready_to_issue"]),
    )
    if exclude_task_id:
        query = query.filter(WorkflowTask.id != exclude_task_id)
    return any(
        _product_context_matches(
            product_context,
            (candidate.payload or {}).get("product_context") or {},
        )
        for candidate in query.all()
    )


def _active_primary_testing_exists(db: Session, order_id: int, exclude_task_id: int | None = None) -> bool:
    query = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order_id,
        WorkflowTask.type == "tester_check",
        WorkflowTask.status.in_(["assigned", "open", "in_progress", "hold"]),
    )
    if exclude_task_id:
        query = query.filter(WorkflowTask.id != exclude_task_id)
    return any(not bool((task.payload or {}).get("retest")) for task in query.all())


def _take_product_quantity(lines: list[dict], quantity: float) -> list[dict]:
    remaining = float(quantity or 0)
    result = []
    for line in lines:
        taken = min(float(line.get("qty") or 0), remaining)
        if taken > 0:
            result.append({**line, "qty": taken})
            remaining -= taken
        if remaining <= 0:
            break
    return result


def _order_product_lines(db: Session, order_id: int) -> list[dict]:
    order_items = db.query(OrderItem).filter(OrderItem.order_id == order_id).all()
    product_ids = [item.product_id for item in order_items]
    products = {
        product.id: product
        for product in db.query(ProductType).filter(ProductType.id.in_(product_ids)).all()
    } if product_ids else {}
    return [
        {
            "order_item_id": item.id,
            "product_id": item.product_id,
            "product_name": products[item.product_id].name if item.product_id in products else f"Изделие ID {item.product_id}",
            "drawing_number": products[item.product_id].drawing_number if item.product_id in products else None,
            "qty": int(item.quantity or 0),
        }
        for item in order_items
    ]


def _product_document_payload(product: ProductType) -> dict:
    return {
        "product_id": product.id,
        "product_name": product.name,
        "drawing_number": product.drawing_number,
        "revision": product.revision,
        "is_subassembly": product.is_subassembly,
        "attachments": product.attachments or [],
    }


def _collect_product_documents(db: Session, order_id: int) -> list[dict]:
    order_items = db.query(OrderItem).filter(OrderItem.order_id == order_id).all()
    product_ids = [item.product_id for item in order_items]
    seen = set()
    documents = []

    while product_ids:
        product_id = product_ids.pop(0)
        if product_id in seen:
            continue
        seen.add(product_id)
        product = db.query(ProductType).filter(ProductType.id == product_id).first()
        if not product:
            continue
        documents.append(_product_document_payload(product))
        child_ids = [
            row.resource_id
            for row in db.query(ProductBOM).filter(
                ProductBOM.product_id == product_id,
                ProductBOM.resource_type.in_(["product", "subassembly"]),
                ProductBOM.resource_id.isnot(None),
            ).all()
        ]
        product_ids.extend(child_ids)

    return documents


def _collect_product_documents_for_product(db: Session, product_id: int) -> list[dict]:
    seen = set()
    product_ids = [product_id]
    documents = []

    while product_ids:
        current_product_id = product_ids.pop(0)
        if current_product_id in seen:
            continue
        seen.add(current_product_id)
        product = db.query(ProductType).filter(ProductType.id == current_product_id).first()
        if not product:
            continue
        documents.append(_product_document_payload(product))
        child_ids = [
            row.resource_id
            for row in db.query(ProductBOM).filter(
                ProductBOM.product_id == current_product_id,
                ProductBOM.resource_type.in_(["product", "subassembly"]),
                ProductBOM.resource_id.isnot(None),
            ).all()
        ]
        product_ids.extend(child_ids)

    return documents


def _order_bom_component_options(db: Session, order_id: int, product_ids: list[int] | None = None) -> list[dict]:
    options = {}

    def merge_designators(*values: str | None) -> str | None:
        result = []
        seen = set()
        for value in values:
            for designator in str(value or "").split(","):
                normalized = designator.strip()
                key = normalized.casefold()
                if normalized and key not in seen:
                    seen.add(key)
                    result.append(normalized)
        return ", ".join(result) or None

    query = db.query(OrderItem).filter(OrderItem.order_id == order_id)
    if product_ids:
        query = query.filter(OrderItem.product_id.in_(product_ids))
    for item in query.all():
        product = db.query(ProductType).filter(ProductType.id == item.product_id).first()
        device = product.name if product else f"Изделие ID {item.product_id}"

        def add_component(component_id: int, qty: float, designators: str | None = None, assembly: str | None = None):
            if component_id in options:
                options[component_id]["required_qty"] += qty
                if designators:
                    existing = options[component_id].get("designators")
                    options[component_id]["designators"] = merge_designators(existing, designators)
                return
            component = db.query(Component).filter(Component.id == component_id).first()
            options[component_id] = {
                "component_id": component_id,
                "component_name": component.name if component else f"Компонент ID {component_id}",
                "part_number": component.part_number if component else None,
                "category": component.category if component else None,
                "package": component.package if component else None,
                "value": component.value if component else None,
                "required_qty": qty,
                "device": device,
                "assembly": assembly,
                "designators": merge_designators(designators),
            }

        def walk_product(product_id: int, multiplier: float, assembly: str | None = None, visited=None):
            visited = visited or set()
            if product_id in visited:
                return
            visited = visited | {product_id}
            bom_items = db.query(ProductBOM).filter(ProductBOM.product_id == product_id).all()
            children_by_parent = {}
            for bom_item in bom_items:
                if bom_item.parent_id:
                    children_by_parent.setdefault(bom_item.parent_id, []).append(bom_item)

            def walk_item(bom_item: ProductBOM, item_multiplier: float, current_assembly: str | None):
                item_type = bom_item.item_type or ("assembly" if bom_item.resource_type in ["product", "subassembly"] else "component")
                total_qty = float(bom_item.quantity or 0) * item_multiplier
                if item_type == "component" and bom_item.resource_id:
                    add_component(int(bom_item.resource_id), total_qty, bom_item.designators, current_assembly)
                    for alternative in bom_item.alternatives or []:
                        add_component(int(alternative.component_id), total_qty, bom_item.designators, current_assembly)
                    return
                if item_type == "assembly":
                    sub_product = db.query(ProductType).filter(ProductType.id == bom_item.resource_id).first() if bom_item.resource_id else None
                    next_assembly = sub_product.name if sub_product else bom_item.design_name
                    if current_assembly:
                        next_assembly = f"{current_assembly} / {next_assembly}"
                    if sub_product:
                        walk_product(sub_product.id, total_qty, next_assembly, visited)
                    for child in children_by_parent.get(bom_item.id, []):
                        walk_item(child, total_qty, next_assembly)

            for bom_item in bom_items:
                if not bom_item.parent_id:
                    walk_item(bom_item, multiplier, assembly)

        walk_product(item.product_id, float(item.quantity or 0))
    return sorted(options.values(), key=lambda item: (item.get("category") or "", item.get("component_name") or ""))


def normalize_task_daily_progress(task: WorkflowTask):
    payload = task.payload or {}
    progress = list(payload.get("daily_progress") or [])
    if task.type != "assembler_build" or task.status == "done":
        return payload

    start = (task.started_at or task.created_at or utcnow()).date()
    yesterday = utcnow().date() - timedelta(days=1)
    existing_dates = {entry.get("date") for entry in progress}
    current = start
    while current <= yesterday:
        iso_date = current.isoformat()
        if iso_date not in existing_dates:
            progress.append({"date": iso_date, "qty": 0, "comment": "Автоматически: отметка за день не заполнена"})
        current += timedelta(days=1)

    if progress != payload.get("daily_progress"):
        task.payload = {**payload, "daily_progress": progress}
        return task.payload
    return payload


def _user_name(db: Session, user_id: int | None) -> str | None:
    if not user_id:
        return None
    user = db.get(User, user_id)
    return user.full_name or user.username if user else None


def _default_assembly_assignments(task: WorkflowTask, payload: dict, target_qty: float) -> list[dict]:
    existing = payload.get("assembly_assignments") or []
    if existing:
        return existing
    product_context = payload.get("product_context") or {}
    product_lines = payload.get("product_lines") or []
    if not product_lines and product_context:
        product_lines = [{
            "order_item_id": product_context.get("order_item_id"),
            "product_id": product_context.get("product_id"),
            "product_name": product_context.get("product_name"),
            "drawing_number": product_context.get("drawing_number"),
            "qty": product_context.get("qty") or target_qty,
        }]
    if not product_lines:
        product_lines = [{"qty": target_qty}]
    return [
        {
            "id": f"product-{line.get('order_item_id') or line.get('product_id') or index}",
            "order_item_id": line.get("order_item_id"),
            "product_id": line.get("product_id"),
            "product_name": line.get("product_name"),
            "drawing_number": line.get("drawing_number"),
            "user_id": task.assigned_user_id,
            "planned_qty": float(line.get("qty") or 0),
            "produced_qty": 0,
        }
        for index, line in enumerate(product_lines)
    ]


def _merge_assembly_assignments(current: list[dict], incoming: list[dict]) -> list[dict]:
    current_by_id = {item.get("id"): dict(item) for item in current if item.get("id")}
    result = []
    for item in incoming:
        item_id = item.get("id") or str(uuid.uuid4())
        base = current_by_id.get(item_id, {})
        planned_qty = float(item.get("planned_qty") or base.get("planned_qty") or 0)
        result.append({
            **base,
            "id": item_id,
            "order_item_id": item.get("order_item_id") or base.get("order_item_id"),
            "product_id": item.get("product_id") or base.get("product_id"),
            "product_name": item.get("product_name") or base.get("product_name"),
            "drawing_number": item.get("drawing_number") or base.get("drawing_number"),
            "user_id": item.get("user_id") or base.get("user_id"),
            "planned_qty": planned_qty,
            "produced_qty": float(base.get("produced_qty") or 0),
        })
    return result


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
                **material,
                **_component_label(components.get(material["component_id"]), material["component_id"]),
                "component_id": material["component_id"],
                "required_qty": material["qty"],
                "available_qty": available,
                "shortage_qty": shortage_qty,
            })
    return shortages


def reconcile_stock_reservations(db: Session):
    active_order_ids = {
        order.id
        for order in db.query(Order).filter(~Order.status.in_(["Ready To Ship", "Cancelled"])).all()
    }
    reservations = db.query(Reservation).all()
    reserved_by_component = {}
    for reservation in reservations:
        if reservation.order_id not in active_order_ids:
            db.delete(reservation)
            continue
        component_id = int(reservation.component_id)
        reserved_by_component[component_id] = reserved_by_component.get(component_id, 0) + float(reservation.qty or 0)

    for stock in db.query(Stock).filter(Stock.component_id.isnot(None)).all():
        stock.reserved_qty = reserved_by_component.get(int(stock.component_id), 0)


def plan_material_availability(db: Session, materials: list[dict]) -> tuple[list[dict], list[dict]]:
    reconcile_stock_reservations(db)
    component_ids = [int(material["component_id"]) for material in materials]
    components = {
        component.id: component
        for component in db.query(Component).filter(Component.id.in_(component_ids)).all()
    } if component_ids else {}
    available_by_component = {}
    if component_ids:
        for stock in db.query(Stock).filter(Stock.component_id.in_(component_ids)).all():
            available_by_component[int(stock.component_id)] = float(stock.actual_qty or 0) - float(stock.reserved_qty or 0)

    available_lines = []
    shortage_lines = []
    for index, material in enumerate(materials):
        component_id = int(material["component_id"])
        required_qty = float(material.get("qty") or 0)
        free_qty = max(float(available_by_component.get(component_id, 0)), 0)
        allocated_qty = min(required_qty, free_qty)
        if allocated_qty > 0:
            available_lines.append({**material, "component_id": component_id, "qty": allocated_qty})
            available_by_component[component_id] = free_qty - allocated_qty
        shortage_qty = required_qty - allocated_qty
        if shortage_qty > 0:
            shortage_lines.append({
                **material,
                **_component_label(components.get(component_id), component_id),
                "component_id": component_id,
                "qty": shortage_qty,
                "required_qty": required_qty,
                "available_qty": allocated_qty,
                "shortage_qty": shortage_qty,
                "line_uid": _shortage_line_uid(material, index),
            })
    return available_lines, shortage_lines


def _available_materials(materials: list[dict], shortages: list[dict]) -> list[dict]:
    shortage_map = {
        int(line["component_id"]): float(line.get("shortage_qty") or 0)
        for line in shortages
    }
    result = []
    for material in materials:
        qty = float(material.get("qty") or 0) - shortage_map.get(int(material["component_id"]), 0)
        if qty > 0:
            result.append({**material, "component_id": int(material["component_id"]), "qty": qty})
    return result


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
                "line_uid": delivery.get("line_uid"),
                "qty": qty,
                "expected_date": delivery.get("expected_date") or completion_payload.get("expected_date"),
                "invoice": delivery.get("invoice") or completion_payload.get("invoice"),
                "supplier": delivery.get("supplier") or completion_payload.get("supplier"),
                "comment": delivery.get("comment") or completion_payload.get("comment"),
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


def _shortage_line_uid(line: dict, index: int) -> str:
    owner_id = line.get("order_item_id") or line.get("product_id") or "order"
    return str(line.get("line_uid") or f"{owner_id}:{line.get('component_id')}:{index}")


def _with_shortage_line_uids(shortages: list[dict]) -> list[dict]:
    return [{**line, "line_uid": _shortage_line_uid(line, index)} for index, line in enumerate(shortages)]


def _deduplicate_shortage_lines(shortages: list[dict]) -> list[dict]:
    """Collapse the same shortage line when duplicate procurement tasks are merged."""
    unique_lines = []
    index_by_uid = {}
    for line in _with_shortage_line_uids(shortages):
        line_uid = line["line_uid"]
        existing_index = index_by_uid.get(line_uid)
        if existing_index is None:
            index_by_uid[line_uid] = len(unique_lines)
            unique_lines.append(line)
            continue

        existing = unique_lines[existing_index]
        existing_qty = float(existing.get("shortage_qty") or existing.get("qty") or 0)
        incoming_qty = float(line.get("shortage_qty") or line.get("qty") or 0)
        shortage_qty = max(existing_qty, incoming_qty)
        unique_lines[existing_index] = {
            **line,
            **existing,
            "line_uid": line_uid,
            "shortage_qty": shortage_qty,
            "qty": shortage_qty,
        }
    return unique_lines


def _split_delivery_lines(shortages: list[dict], deliveries: list[dict], *,
                          allow_overage: bool = False) -> tuple[list[dict], list[dict]]:
    source_lines = _with_shortage_line_uids(shortages)
    remaining_qty = {
        line["line_uid"]: float(line.get("shortage_qty") or line.get("qty") or 0)
        for line in source_lines
    }
    lines_by_uid = {line["line_uid"]: line for line in source_lines}
    accepted = []

    for delivery in deliveries:
        qty_left = float(delivery.get("qty") or 0)
        if qty_left <= 0:
            continue
        accepted_start = len(accepted)
        delivery_uid = delivery.get("line_uid")
        candidate_lines = (
            [lines_by_uid[delivery_uid]]
            if delivery_uid and delivery_uid in lines_by_uid
            else [line for line in source_lines if int(line["component_id"]) == int(delivery["component_id"])]
        )
        for line in candidate_lines:
            if qty_left <= 0:
                break
            line_uid = line["line_uid"]
            line_remaining = remaining_qty.get(line_uid, 0)
            if line_remaining <= 0:
                continue
            qty = min(qty_left, line_remaining)
            accepted.append({
                **line,
                "qty": qty,
                "shortage_qty": qty,
                "expected_date": delivery.get("expected_date"),
                "invoice": delivery.get("invoice"),
                "supplier": delivery.get("supplier"),
                "comment": delivery.get("comment"),
            })
            remaining_qty[line_uid] = line_remaining - qty
            qty_left -= qty
        if allow_overage and qty_left > 0 and candidate_lines:
            if len(accepted) > accepted_start:
                accepted[-1]["qty"] = float(accepted[-1]["qty"]) + qty_left
                accepted[-1]["shortage_qty"] = float(accepted[-1]["shortage_qty"]) + qty_left
            else:
                line = candidate_lines[0]
                accepted.append({
                    **line,
                    "qty": qty_left,
                    "shortage_qty": qty_left,
                    "expected_date": delivery.get("expected_date"),
                    "invoice": delivery.get("invoice"),
                    "supplier": delivery.get("supplier"),
                    "comment": delivery.get("comment"),
                })

    remaining = []
    for line in source_lines:
        qty = remaining_qty.get(line["line_uid"], 0)
        if qty > 0:
            remaining.append({**line, "qty": qty, "shortage_qty": qty})
    return accepted, remaining


def _receive_components_to_stock(db: Session, lines: list[dict], task: WorkflowTask | None = None,
                                 actor_user_id: int | None = None):
    for line in lines:
        component_id = int(line["component_id"])
        qty = float(line.get("qty") or line.get("shortage_qty") or 0)
        if qty <= 0:
            continue
        stock = db.query(Stock).filter(Stock.component_id == component_id).with_for_update().first()
        if stock:
            stock.actual_qty = (stock.actual_qty or 0) + qty
        else:
            stock = Stock(component_id=component_id, actual_qty=qty, location="Warehouse-1")
            db.add(stock)
        record_movement(
            db,
            direction="incoming",
            quantity=qty,
            balance_after=stock.actual_qty,
            component_id=component_id,
            location=stock.location,
            task_id=task.id if task else None,
            order_id=task.order_id if task else None,
            actor_user_id=actor_user_id,
            recipient=(task.payload or {}).get("supplier") if task else None,
            note="Приёмка комплектующих на склад",
        )


def _update_procurement_purchase_receipt(db: Session, payload: dict, received: list[dict]):
    procurement_task_id = payload.get("procurement_task_id")
    if not procurement_task_id:
        return

    procurement_task = db.query(WorkflowTask).filter(WorkflowTask.id == procurement_task_id).first()
    if not procurement_task:
        return

    procurement_payload = procurement_task.payload or {}
    received_by_purchase = {}
    legacy_purchase_id = payload.get("purchase_id")
    if legacy_purchase_id:
        received_by_purchase[str(legacy_purchase_id)] = sum(float(line.get("qty") or line.get("shortage_qty") or 0) for line in received)
    else:
        for line in received:
            purchase_id = line.get("purchase_id")
            if not purchase_id:
                continue
            received_by_purchase[str(purchase_id)] = received_by_purchase.get(str(purchase_id), 0) + float(line.get("qty") or line.get("shortage_qty") or 0)
    if not received_by_purchase:
        return

    purchases = []
    for purchase in procurement_payload.get("purchases", []):
        received_qty = received_by_purchase.get(str(purchase.get("id")), 0)
        if received_qty:
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
        WorkflowTask.status.in_(["assigned", "in_progress", "open", "waiting_delivery", "hold", "ready_to_issue"]),
    ).first() is not None


def _pending_incoming_qty_map(db: Session, order_id: int, exclude_task_id: int | None = None) -> dict[int, float]:
    result = {}
    query = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order_id,
        WorkflowTask.type.in_(["accounting_payment", "warehouse_receive_components"]),
        WorkflowTask.status.in_(["assigned", "in_progress", "open", "waiting_delivery", "hold", "ready_to_issue"]),
    )
    if exclude_task_id:
        query = query.filter(WorkflowTask.id != exclude_task_id)

    for task in query.all():
        for line in (task.payload or {}).get("shortages", []):
            component_id = int(line["component_id"])
            qty = float(line.get("shortage_qty") or line.get("qty") or 0)
            if qty > 0:
                result[component_id] = result.get(component_id, 0) + qty
    return result


def _uncovered_shortages(db: Session, order_id: int, shortages: list[dict],
                         exclude_task_id: int | None = None) -> list[dict]:
    pending = _pending_incoming_qty_map(db, order_id, exclude_task_id=exclude_task_id)
    result = []
    for shortage in shortages:
        component_id = int(shortage["component_id"])
        shortage_qty = float(shortage.get("shortage_qty") or shortage.get("qty") or 0)
        covered_by_incoming = min(shortage_qty, float(pending.get(component_id, 0)))
        uncovered_qty = shortage_qty - covered_by_incoming
        if uncovered_qty > 0:
            result.append({
                **shortage,
                "shortage_qty": uncovered_qty,
                "pending_incoming_qty": covered_by_incoming,
            })
    return result


def _reserve_materials_for_issue(db: Session, order_id: int, materials: list[dict], *,
                                 title: str, description: str, partial: bool,
                                 product_context: dict | None = None):
    if not materials:
        return
    if not product_context and _open_task_exists(db, order_id, "warehouse_issue_materials"):
        return

    reserve_components(db, materials)
    for material in materials:
        db.add(Reservation(
            order_id=order_id,
            component_id=material["component_id"],
            qty=material["qty"],
        ))
    create_task(
        db,
        order_id=order_id,
        task_type="warehouse_issue_materials",
        title=title,
        role="warehouse",
        description=description,
        payload={
            **({"product_context": product_context} if product_context else {}),
            "materials": enrich_component_lines(db, materials),
            "partial": partial,
        },
    )


def create_procurement_task_for_order(db: Session, order: Order, shortages: list[dict]):
    shortages = _with_shortage_line_uids(shortages)
    if not shortages:
        return None
    if _open_task_exists(db, order.id, "procurement_purchase"):
        return None
    order.status = "Procurement Required"
    return create_task(
        db,
        order_id=order.id,
        task_type="procurement_purchase",
        title=f"Закупить комплектующие по заказу #{order.id}",
        role="procurement",
        description="Оформить закупку недостающих комплектующих и передать счет бухгалтерии.",
        payload={"shortages": shortages},
    )


def _product_context_from_line(line: dict) -> dict | None:
    if not line.get("product_id") and not line.get("order_item_id"):
        return None
    return {
        "order_item_id": line.get("order_item_id"),
        "product_id": line.get("product_id"),
        "product_name": line.get("product_name"),
        "drawing_number": line.get("drawing_number"),
        "qty": line.get("product_qty") or line.get("qty"),
    }


def _group_lines_by_product_context(lines: list[dict]) -> list[tuple[dict | None, list[dict]]]:
    groups = {}
    for line in lines:
        context = _product_context_from_line(line)
        key = (
            context.get("order_item_id") if context else None,
            context.get("product_id") if context else None,
        )
        if key not in groups:
            groups[key] = {"context": context, "lines": []}
        groups[key]["lines"].append(line)
    return [(group["context"], group["lines"]) for group in groups.values()]


def _create_repair_material_flow(db: Session, task: WorkflowTask, requested_materials: list[dict]) -> dict:
    payload = task.payload or {}
    product_context = payload.get("product_context")
    product_ids = [int(product_context["product_id"])] if product_context and product_context.get("product_id") else None
    component_options = (
        _order_bom_component_options(db, task.order_id, product_ids=product_ids)
        if product_ids
        else (payload.get("component_options") or _order_bom_component_options(db, task.order_id))
    )
    allowed_component_ids = {
        int(item["component_id"])
        for item in component_options
        if item.get("component_id")
    }
    materials = [
        {
            **(product_context or {}),
            "component_id": int(item["component_id"]),
            "qty": float(item.get("qty") or 0),
            "reason": item.get("reason") or "",
        }
        for item in requested_materials or []
        if item.get("component_id")
        and float(item.get("qty") or 0) > 0
        and (not allowed_component_ids or int(item["component_id"]) in allowed_component_ids)
    ]
    rejected = [
        item for item in requested_materials or []
        if item.get("component_id") and allowed_component_ids and int(item["component_id"]) not in allowed_component_ids
    ]
    if rejected and allowed_component_ids:
        raise HTTPException(status_code=400, detail="Компонент не найден в составе изделия")
    if not materials:
        return {"created": False}
    if any(not material.get("reason") for material in materials):
        raise HTTPException(status_code=400, detail="Укажите обоснование для запроса дополнительных компонентов")
    request_reason = "; ".join(
        sorted({material.get("reason") for material in materials if material.get("reason")})
    )

    requested_component_ids = {int(material["component_id"]) for material in materials}
    active_statuses = ["assigned", "in_progress", "open", "waiting_delivery", "hold", "ready_to_issue"]
    active_children = db.query(WorkflowTask).filter(
        WorkflowTask.status.in_(active_statuses),
        WorkflowTask.type.in_(["repair_issue_materials", "repair_receive_materials", "procurement_purchase", "accounting_payment", "warehouse_receive_components"]),
    ).all()
    for child_task in active_children:
        child_payload = child_task.payload or {}
        if int(child_payload.get("source_task_id") or 0) != int(task.id):
            continue
        child_lines = [*(child_payload.get("materials") or []), *(child_payload.get("shortages") or [])]
        child_component_ids = {int(line["component_id"]) for line in child_lines if line.get("component_id")}
        if requested_component_ids.intersection(child_component_ids):
            raise HTTPException(status_code=400, detail="По этому компоненту уже есть активная заявка на выдачу или закупку")

    shortages = find_shortages(db, materials)
    available_materials = _available_materials(materials, shortages)
    source_label = "сборки" if task.type == "assembler_build" else "ремонта"
    source_role = "assembler" if task.type == "assembler_build" else "repair_engineer"
    issue_task_ids = []
    procurement_task_ids = []
    if available_materials:
        reserve_components(db, available_materials)
        for material in available_materials:
            db.add(Reservation(
                order_id=task.order_id,
                component_id=material["component_id"],
                qty=material["qty"],
            ))
        issue_task = create_task(
            db,
            order_id=task.order_id,
            task_type="repair_issue_materials",
            title=f"Выдать доп. компоненты для {source_label} по заказу #{task.order_id}",
            role="warehouse",
            description=f"Выдать дополнительные компоненты для устранения брака на этапе {source_label}.",
            payload={
                **({"product_context": product_context} if product_context else {}),
                "source_task_id": task.id,
                "source_task_type": task.type,
                "counterparty_role": source_role,
                "request_reason": request_reason,
                "materials": enrich_component_lines(db, available_materials),
                "partial": bool(shortages),
            },
        )
        issue_task_ids.append(issue_task.id)

    if shortages:
        procurement_task = create_task(
            db,
            order_id=task.order_id,
            task_type="procurement_purchase",
            title=f"Закупить дополнительные компоненты для {source_label} по заказу #{task.order_id}",
            role="procurement",
            description=f"Закупить дополнительные компоненты, необходимые для устранения брака на этапе {source_label}.",
            payload={
                **({"product_context": product_context} if product_context else {}),
                "shortages": shortages,
                "source_task_id": task.id,
                "source_task_type": task.type,
                "request_reason": request_reason,
                "purpose": "assembly" if task.type == "assembler_build" else "repair",
            },
        )
        procurement_task_ids.append(procurement_task.id)

    requests = list(payload.get("material_requests") or [])
    requests.append({
        "created_at": utcnow().isoformat(),
        "items": enrich_component_lines(db, materials),
        "available": enrich_component_lines(db, available_materials),
        "shortages": shortages,
        "request_reason": request_reason,
        "issue_task_ids": issue_task_ids,
        "procurement_task_ids": procurement_task_ids,
    })
    task.payload = {**payload, "material_requests": requests}
    return {
        "created": True,
        "available": available_materials,
        "shortages": shortages,
        "issue_task_ids": issue_task_ids,
        "procurement_task_ids": procurement_task_ids,
    }


def _open_repair_material_flow_exists(db: Session, repair_task_id: int) -> bool:
    active_statuses = ["assigned", "in_progress", "open", "waiting_delivery", "hold", "ready_to_issue"]
    tasks = db.query(WorkflowTask).filter(
        WorkflowTask.status.in_(active_statuses),
        WorkflowTask.type.in_(["repair_issue_materials", "repair_receive_materials", "procurement_purchase", "accounting_payment", "warehouse_receive_components"]),
    ).all()
    return any(int((task.payload or {}).get("source_task_id") or 0) == repair_task_id for task in tasks)


def _open_material_flow_tasks(db: Session, source_task_id: int) -> list[dict]:
    active_statuses = ["assigned", "in_progress", "open", "waiting_delivery", "hold", "ready_to_issue"]
    tasks = db.query(WorkflowTask).filter(
        WorkflowTask.status.in_(active_statuses),
        WorkflowTask.type.in_(["repair_issue_materials", "repair_receive_materials", "procurement_purchase", "accounting_payment", "warehouse_receive_components"]),
    ).order_by(WorkflowTask.id.asc()).all()
    result = []
    for child_task in tasks:
        payload = child_task.payload or {}
        if int(payload.get("source_task_id") or 0) != int(source_task_id):
            continue
        result.append({
            "id": child_task.id,
            "type": child_task.type,
            "status": child_task.status,
            "role": child_task.role,
            "title": child_task.title,
            "materials": enrich_component_lines(db, payload.get("materials") or payload.get("shortages") or []),
            "request_reason": payload.get("request_reason"),
        })
    return result


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


def _issued_qty_map(db: Session, order_id: int) -> dict[int, float]:
    result = {}
    movements = db.query(InventoryMovement).filter(
        InventoryMovement.order_id == order_id,
        InventoryMovement.direction == "outgoing",
        InventoryMovement.counterparty_role == "assembler",
        InventoryMovement.component_id.isnot(None),
    ).all()
    for movement in movements:
        component_id = int(movement.component_id)
        result[component_id] = result.get(component_id, 0) + float(movement.quantity or 0)

    if result:
        return result

    tasks = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order_id,
        WorkflowTask.type == "warehouse_issue_materials",
        WorkflowTask.status == "done",
    ).all()
    for task in tasks:
        for line in (task.payload or {}).get("materials", []):
            component_id = int(line["component_id"])
            result[component_id] = result.get(component_id, 0) + float(line.get("qty") or 0)
    return result


def _issued_qty_map_for_product(db: Session, order_id: int, product_context: dict) -> dict[int, float]:
    result = {}
    order_item_id = product_context.get("order_item_id")
    product_id = product_context.get("product_id")
    tasks = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order_id,
        WorkflowTask.type == "warehouse_issue_materials",
        WorkflowTask.status == "done",
    ).all()
    for task in tasks:
        context = (task.payload or {}).get("product_context") or {}
        if order_item_id and context.get("order_item_id") != order_item_id:
            continue
        if not order_item_id and product_id and context.get("product_id") != product_id:
            continue
        for line in (task.payload or {}).get("materials", []):
            component_id = int(line["component_id"])
            result[component_id] = result.get(component_id, 0) + float(line.get("qty") or 0)
    return result


def _product_context_matches(expected: dict, actual: dict) -> bool:
    order_item_id = expected.get("order_item_id")
    product_id = expected.get("product_id")
    if order_item_id:
        return actual.get("order_item_id") == order_item_id
    if product_id:
        return actual.get("product_id") == product_id
    return False


def _normalize_product_context_qty(db: Session, order_id: int, product_context: dict | None) -> dict | None:
    if not product_context:
        return product_context
    result = dict(product_context)
    order_item_id = result.get("order_item_id")
    if order_item_id:
        order_item = db.query(OrderItem).filter(
            OrderItem.id == int(order_item_id),
            OrderItem.order_id == order_id,
        ).first()
        if order_item:
            result["qty"] = float(order_item.quantity or 0)
            return result
    return result


def _accepted_complete_kit_qty_for_product(db: Session, order_id: int, product_context: dict) -> float:
    """Count complete kits accepted by assembly before possible BOM changes."""
    target_qty = float(product_context.get("qty") or 0)

    def count_complete_kits(task_type: str) -> float:
        qty = 0.0
        tasks = db.query(WorkflowTask).filter(
            WorkflowTask.order_id == order_id,
            WorkflowTask.type == task_type,
            WorkflowTask.status == "done",
        ).all()
        for task in tasks:
            payload = task.payload or {}
            context = payload.get("product_context") or {}
            if not _product_context_matches(product_context, context):
                continue
            if payload.get("partial"):
                continue
            if not payload.get("materials"):
                continue
            qty += float(context.get("qty") or target_qty or 0)
        return qty

    accepted_qty = count_complete_kits("assembler_receive_materials")
    if accepted_qty > 0:
        return min(accepted_qty, target_qty) if target_qty > 0 else accepted_qty

    issued_qty = count_complete_kits("warehouse_issue_materials")
    return min(issued_qty, target_qty) if target_qty > 0 else issued_qty


def _tester_task_exists_for_context(db: Session, order_id: int, product_context: dict) -> bool:
    tasks = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order_id,
        WorkflowTask.type == "tester_check",
        WorkflowTask.status.in_(["assigned", "open", "in_progress", "hold", "done"]),
    ).all()
    for task in tasks:
        context = (task.payload or {}).get("product_context") or {}
        if _product_context_matches(product_context, context):
            return True
    return False


def _product_test_checklist(db: Session, product_id: int | None) -> list[dict]:
    if not product_id:
        return []
    product = db.query(ProductType).filter(ProductType.id == int(product_id)).first()
    if not product:
        return []
    result = []
    for index, item in enumerate(product.test_checklist or []):
        label = (item.get("label") if isinstance(item, dict) else str(item)).strip()
        if label:
            result.append({"id": f"check-{index + 1}", "label": label, "checked": False})
    return result


def _bom_requirement_groups(db: Session, product_id: int, multiplier: float = 1, assembly: str | None = None,
                            visited: set[int] | None = None) -> list[dict]:
    visited = visited or set()
    if product_id in visited:
        return []
    visited = visited | {product_id}
    result = []
    bom_items = db.query(ProductBOM).filter(ProductBOM.product_id == product_id).all()
    children_by_parent = {}
    for item in bom_items:
        if item.parent_id:
            children_by_parent.setdefault(item.parent_id, []).append(item)

    def walk_item(item: ProductBOM, item_multiplier: float, current_assembly: str | None):
        item_type = item.item_type or ("assembly" if item.resource_type in ["product", "subassembly"] else "component")
        total_qty = float(item.quantity or 0) * item_multiplier
        if total_qty <= 0:
            return
        if item_type == "component":
            allowed_ids = []
            if item.resource_id:
                allowed_ids.append(int(item.resource_id))
            for alternative in item.alternatives or []:
                component_id = int(alternative.component_id)
                if component_id not in allowed_ids:
                    allowed_ids.append(component_id)
            if allowed_ids:
                result.append({
                    "allowed_component_ids": allowed_ids,
                    "qty": total_qty,
                    "design_name": item.design_name,
                    "designators": item.designators,
                    "assembly": current_assembly,
                })
            return
        if item_type == "assembly":
            sub_product = db.query(ProductType).filter(ProductType.id == item.resource_id).first() if item.resource_id else None
            next_assembly = sub_product.name if sub_product else item.design_name
            if current_assembly:
                next_assembly = f"{current_assembly} / {next_assembly}"
            if sub_product:
                result.extend(_bom_requirement_groups(db, sub_product.id, total_qty, next_assembly, visited))
            for child in children_by_parent.get(item.id, []):
                walk_item(child, total_qty, next_assembly)

    for bom_item in bom_items:
        if not bom_item.parent_id:
            walk_item(bom_item, multiplier, assembly)
    return result


def _buildable_qty_from_issued(db: Session, order_id: int, product_context: dict) -> dict:
    snapshot_qty = _accepted_complete_kit_qty_for_product(db, order_id, product_context)
    product_id = product_context.get("product_id")
    if not product_id:
        qty = float(product_context.get("qty") or 0)
        return {"qty": max(qty, snapshot_qty), "lines": [], "blockers": [], "snapshot_qty": snapshot_qty}
    requirement_groups = _bom_requirement_groups(db, int(product_id), 1)
    if not requirement_groups:
        qty = float(product_context.get("qty") or 0)
        return {"qty": max(qty, snapshot_qty), "lines": [], "blockers": [], "snapshot_qty": snapshot_qty}
    issued = _issued_qty_map_for_product(db, order_id, product_context)
    component_ids = sorted({component_id for group in requirement_groups for component_id in group["allowed_component_ids"]})
    components = {
        component.id: component
        for component in db.query(Component).filter(Component.id.in_(component_ids)).all()
    } if component_ids else {}
    lines = []
    possible_qty = []
    for group in requirement_groups:
        required = float(group.get("qty") or 0)
        if required <= 0:
            continue
        issued_qty = sum(float(issued.get(component_id, 0)) for component_id in group["allowed_component_ids"])
        buildable_qty = issued_qty // required
        possible_qty.append(buildable_qty)
        component_names = [
            components[component_id].name if component_id in components else f"Компонент ID {component_id}"
            for component_id in group["allowed_component_ids"]
        ]
        lines.append({
            "component_ids": group["allowed_component_ids"],
            "component_name": " / ".join(component_names),
            "design_name": group.get("design_name"),
            "designators": group.get("designators"),
            "assembly": group.get("assembly"),
            "required_per_unit": required,
            "issued_qty": issued_qty,
            "buildable_qty": buildable_qty,
            "missing_qty_for_one": max(required - issued_qty, 0),
        })
    live_qty = min(possible_qty) if possible_qty else float(product_context.get("qty") or 0)
    qty = live_qty
    snapshot_used = False
    return {
        "qty": qty,
        "lines": lines,
        "blockers": [] if snapshot_used else [
            line for line in lines if line["buildable_qty"] <= qty and line["missing_qty_for_one"] > 0
        ],
        "snapshot_qty": snapshot_qty,
        "snapshot_used": snapshot_used,
    }


def _max_buildable_qty_from_issued(db: Session, order_id: int, product_context: dict) -> float:
    return float(_buildable_qty_from_issued(db, order_id, product_context).get("qty") or 0)


def _remaining_order_materials(db: Session, order_id: int) -> list[dict]:
    issued = _issued_qty_map(db, order_id)
    remaining = []
    for material in _calculate_order_materials(db, order_id):
        qty = max(float(material["qty"]) - issued.get(int(material["component_id"]), 0), 0)
        if qty > 0:
            remaining.append({"component_id": int(material["component_id"]), "qty": qty})
    return remaining


def get_order_remaining_materials(db: Session, order_id: int) -> list[dict]:
    """Public read model for requirements not yet physically issued to assembly."""
    return _remaining_order_materials(db, order_id)


def find_order_shortages(db: Session, order_id: int) -> list[dict]:
    materials = _remaining_order_materials(db, order_id)
    component_ids = [material["component_id"] for material in materials]
    components = {
        component.id: component
        for component in db.query(Component).filter(Component.id.in_(component_ids)).all()
    } if component_ids else {}
    own_reservations = {}
    for reservation in db.query(Reservation).filter(Reservation.order_id == order_id).all():
        own_reservations[reservation.component_id] = own_reservations.get(reservation.component_id, 0) + float(reservation.qty)

    shortages = []
    for material in materials:
        component_id = int(material["component_id"])
        stock = db.query(Stock).filter(Stock.component_id == component_id).first()
        actual_qty = float(stock.actual_qty or 0) if stock else 0
        total_reserved = float(stock.reserved_qty or 0) if stock else 0
        available_for_order = max(actual_qty - total_reserved + own_reservations.get(component_id, 0), 0)
        shortage_qty = float(material["qty"]) - available_for_order
        if shortage_qty > 0:
            shortages.append({
                **_component_label(components.get(component_id), component_id),
                "qty": shortage_qty,
                "required_qty": float(material["qty"]),
                "available_qty": available_for_order,
                "shortage_qty": shortage_qty,
            })
    return shortages


def _order_target_qty(db: Session, order_id: int) -> float:
    return sum(
        float(item.quantity or 0)
        for item in db.query(OrderItem).filter(OrderItem.order_id == order_id).all()
    )


def _assembly_materials_complete(db: Session, order_id: int, exclude_receipt_id: int | None = None) -> bool:
    if _remaining_order_materials(db, order_id):
        return False
    query = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order_id,
        WorkflowTask.type == "assembler_receive_materials",
        WorkflowTask.status.in_(["assigned", "in_progress", "open", "hold"]),
    )
    if exclude_receipt_id:
        query = query.filter(WorkflowTask.id != exclude_receipt_id)
    return query.first() is None


def ensure_assembly_device_pool(db: Session, task: WorkflowTask) -> WorkflowTask:
    """Create and attach the unassigned device pool for a regular assembly task."""
    if task.type != "assembler_build" or not task.order_id:
        return task

    payload = task.payload or {}
    context = payload.get("product_context") or {}
    product_id = context.get("product_id")
    order_item_id = context.get("order_item_id")
    planned_qty = max(int(float(payload.get("planned_qty") or context.get("qty") or 0)), 0)
    if not product_id or planned_qty <= 0:
        return task

    units = (
        db.query(Item)
        .filter(Item.assembly_task_id == task.id)
        .order_by(Item.id.asc())
        .all()
    )
    missing_qty = planned_qty - len(units)
    if missing_qty > 0:
        product = db.query(ProductType).filter(ProductType.id == int(product_id)).first()
        if not product:
            return task
        units.extend(create_product_units(
            db,
            order_id=task.order_id,
            order_item_id=int(order_item_id) if order_item_id else None,
            product=product,
            assembly_task_id=task.id,
            assigned_user_id=None,
            quantity=missing_qty,
        ))

    valid_serials = {unit.serial_number for unit in units}
    claims = {
        serial_number: user_id
        for serial_number, user_id in (payload.get("assembly_claims") or {}).items()
        if serial_number in valid_serials
    }
    task.payload = {
        **payload,
        "unit_ids": [unit.id for unit in units],
        "serial_numbers": [unit.serial_number for unit in units],
        "assembly_claims": claims,
    }
    product = db.query(ProductType).filter(ProductType.id == int(product_id)).first()
    if product and product.requires_preassembly_test and not task.payload.get("preassembly_test_completed"):
        existing_pretest = db.query(WorkflowTask).filter(
            WorkflowTask.order_id == task.order_id,
            WorkflowTask.type == "tester_check",
        ).all()
        existing_pretest = next((
            candidate for candidate in existing_pretest
            if (candidate.payload or {}).get("pre_assembly")
            and int((candidate.payload or {}).get("source_assembly_task_id") or 0) == task.id
            and candidate.status in ["assigned", "open", "in_progress", "hold"]
        ), None)
        if not existing_pretest:
            checklist = _product_test_checklist(db, product.id)
            for unit in units:
                if unit.status in ["planned", "in_assembly"]:
                    unit.status = "testing"
            existing_pretest = create_task(
                db,
                order_id=task.order_id,
                task_type="tester_check",
                title=f"Предварительно протестировать {product.name} по заказу #{task.order_id}",
                role="tester",
                description="Проверить устройства до установки в корпус. Годные устройства вернутся в пул сборки.",
                payload={
                    "product_context": context,
                    "source_assembly_task_id": task.id,
                    "pre_assembly": True,
                    "planned_qty": planned_qty,
                    "product_lines": [{
                        "order_item_id": order_item_id,
                        "product_id": product.id,
                        "product_name": product.name,
                        "drawing_number": product.drawing_number,
                        "qty": planned_qty,
                        "test_checklist": checklist,
                    }],
                    "test_checklist": checklist,
                    "unit_ids": [unit.id for unit in units],
                    "serial_numbers": [unit.serial_number for unit in units],
                },
            )
        passed_serials = task.payload.get("preassembly_passed_serial_numbers") or []
        if not passed_serials:
            task.status = "hold"
        task.payload = {
            **task.payload,
            "preassembly_test_required": True,
            "preassembly_test_task_id": existing_pretest.id,
            "blocked_reason": (
                None
                if passed_serials
                else "Ожидается предварительное тестирование до сборки в корпус"
            ),
        }
    return task


def _ensure_assembly_task(db: Session, order: Order, materials_complete: bool,
                          product_context: dict | None = None) -> WorkflowTask:
    product_context = _normalize_product_context_qty(db, order.id, product_context)
    target_qty = float(product_context.get("qty") or 0) if product_context else _order_target_qty(db, order.id)
    product_name = product_context.get("product_name") if product_context else None
    context_label = f" · {product_name}" if product_name else ""
    product_lines = [{
        "order_item_id": product_context.get("order_item_id"),
        "product_id": product_context.get("product_id"),
        "product_name": product_context.get("product_name"),
        "drawing_number": product_context.get("drawing_number"),
        "qty": int(product_context.get("qty") or target_qty or 0),
    }] if product_context else _order_product_lines(db, order.id)
    product_documents = (
        _collect_product_documents_for_product(db, int(product_context["product_id"]))
        if product_context and product_context.get("product_id")
        else _collect_product_documents(db, order.id)
    )
    component_options = _order_bom_component_options(
        db,
        order.id,
        product_ids=[int(product_context["product_id"])] if product_context and product_context.get("product_id") else None,
    )
    tasks = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order.id,
        WorkflowTask.type == "assembler_build",
        WorkflowTask.status.in_(["assigned", "in_progress", "open", "hold", "ready_to_issue"]),
    ).all()
    if product_context:
        for candidate in tasks:
            candidate_payload = candidate.payload or {}
            if not candidate_payload.get("product_context") and len(candidate_payload.get("product_lines") or []) > 1:
                candidate.status = "done"
                candidate.completed_at = candidate.completed_at or utcnow()
                candidate.payload = {
                    **candidate_payload,
                    "replaced_by_product_tasks": True,
                    "replacement_note": "Заменена отдельными задачами сборки по изделиям заказа",
                }
    task = None
    if product_context:
        order_item_id = product_context.get("order_item_id")
        product_id = product_context.get("product_id")
        for candidate in tasks:
            candidate_context = (candidate.payload or {}).get("product_context") or {}
            if order_item_id and candidate_context.get("order_item_id") == order_item_id:
                task = candidate
                break
            if not order_item_id and product_id and candidate_context.get("product_id") == product_id:
                task = candidate
                break
    else:
        task = next((candidate for candidate in tasks if not (candidate.payload or {}).get("product_context")), None)
    if task:
        existing_payload = task.payload or {}
        existing_context = existing_payload.get("product_context") or {}
        if product_context and existing_context:
            product_context = {**product_context, "qty": max(float(existing_context.get("qty") or 0), target_qty)}
            target_qty = float(product_context.get("qty") or target_qty)
            product_lines = [{**line, "qty": target_qty} for line in product_lines]
        task.payload = {
            **existing_payload,
            **({"product_context": product_context} if product_context else {}),
            "product_lines": product_lines,
            "planned_qty": target_qty,
            "materials_complete": materials_complete,
            "product_documents": product_documents,
            "component_options": component_options,
        }
        task.title = f"Сборка изделия по заказу #{order.id}{context_label}"
        task.description = (
            "Вести журнал выпуска по этому изделию в пределах выданных комплектов."
            if not materials_complete
            else "Отметить выпуск изделий и передать готовые устройства на тестирование."
        )
        return ensure_assembly_device_pool(db, task)

    task = create_task(
        db,
        order_id=order.id,
        task_type="assembler_build",
        title=f"Сборка изделия по заказу #{order.id}{context_label}",
        role="assembler",
        description=(
            "Вести журнал выпуска по этому изделию в пределах выданных комплектов."
            if not materials_complete
            else "Отметить выпуск изделий и передать готовые устройства на тестирование."
        ),
        payload={
            **({"product_context": product_context} if product_context else {}),
            "product_lines": product_lines,
            "planned_qty": target_qty,
            "materials_complete": materials_complete,
            "started_qty": 0,
            "daily_progress": [],
            "product_documents": product_documents,
            "component_options": component_options,
        },
    )
    return ensure_assembly_device_pool(db, task)


def normalize_assembly_task_payload(db: Session, task: WorkflowTask) -> dict:
    payload = normalize_task_daily_progress(task)
    if task.type != "assembler_build" or not task.order_id:
        return payload

    product_lines = _order_product_lines(db, task.order_id)
    if not product_lines:
        return payload

    existing_context = payload.get("product_context") or {}
    existing_key = existing_context.get("order_item_id") or existing_context.get("product_id")
    if existing_context:
        line = next((item for item in product_lines if (item.get("order_item_id") or item.get("product_id")) == existing_key), None)
        buildable = _buildable_qty_from_issued(db, task.order_id, existing_context)
        issued_qty = float(buildable.get("qty") or 0)
        context_product_ids = [int(existing_context["product_id"])] if existing_context.get("product_id") else None
        return {
            **payload,
            "product_lines": [line] if line else payload.get("product_lines") or [],
            "planned_qty": float(existing_context.get("qty") or (line or {}).get("qty") or payload.get("planned_qty") or 0),
            "issued_qty": issued_qty,
            "issued_details": buildable,
            "open_material_flow": _open_material_flow_tasks(db, task.id),
            "product_documents": payload.get("product_documents") or (
                _collect_product_documents_for_product(db, int(existing_context["product_id"]))
                if existing_context.get("product_id")
                else []
            ),
            "component_options": _order_bom_component_options(
                db,
                task.order_id,
                product_ids=context_product_ids,
            ),
        }
    existing_assignments = payload.get("assembly_assignments") or []
    assignments = []
    used_keys = set()

    for assignment in existing_assignments:
        assignment_key = assignment.get("order_item_id") or assignment.get("product_id") or existing_key
        line = next((item for item in product_lines if (item.get("order_item_id") or item.get("product_id")) == assignment_key), None)
        if line:
            used_keys.add(assignment_key)
            assignments.append({
                **assignment,
                "order_item_id": line.get("order_item_id"),
                "product_id": line.get("product_id"),
                "product_name": line.get("product_name"),
                "drawing_number": line.get("drawing_number"),
            })
        else:
            assignments.append(assignment)

    for index, line in enumerate(product_lines):
        key = line.get("order_item_id") or line.get("product_id")
        if key in used_keys:
            continue
        assignments.append({
            "id": f"product-{line.get('order_item_id') or line.get('product_id') or index}",
            "order_item_id": line.get("order_item_id"),
            "product_id": line.get("product_id"),
            "product_name": line.get("product_name"),
            "drawing_number": line.get("drawing_number"),
            "user_id": None,
            "planned_qty": float(line.get("qty") or 0),
            "produced_qty": 0,
        })

    devices = []
    existing_devices = {
        item.get("order_item_id") or item.get("product_id"): item
        for item in payload.get("assembly_devices") or []
    }
    for line in product_lines:
        key = line.get("order_item_id") or line.get("product_id")
        device_complete = bool((existing_devices.get(key) or {}).get("materials_complete"))
        if existing_key and key == existing_key:
            device_complete = bool(payload.get("materials_complete"))
        devices.append({**line, "materials_complete": device_complete})

    next_payload = {
        **payload,
        "product_context": None,
        "product_lines": product_lines,
        "assembly_devices": devices,
        "planned_qty": _order_target_qty(db, task.order_id),
        "materials_complete": bool(devices) and all(item.get("materials_complete") for item in devices),
        "assembly_assignments": assignments,
        "open_material_flow": _open_material_flow_tasks(db, task.id),
        "product_documents": payload.get("product_documents") or _collect_product_documents(db, task.order_id),
        "component_options": payload.get("component_options") or _order_bom_component_options(db, task.order_id),
    }
    if next_payload != payload:
        task.payload = next_payload
    return next_payload


def split_aggregate_assembly_tasks(db: Session):
    tasks = db.query(WorkflowTask).filter(
        WorkflowTask.type == "assembler_build",
        WorkflowTask.status.in_(["assigned", "in_progress", "open", "hold"]),
    ).all()
    for task in tasks:
        payload = task.payload or {}
        if payload.get("product_context"):
            continue
        product_lines = payload.get("product_lines") or _order_product_lines(db, task.order_id)
        if len(product_lines) <= 1:
            continue
        has_progress = bool(payload.get("daily_progress")) or any(
            float(item.get("produced_qty") or 0) > 0
            for item in payload.get("assembly_assignments") or []
        )
        if has_progress:
            continue
        order = db.query(Order).filter(Order.id == task.order_id).first()
        if not order:
            continue
        devices = {
            item.get("order_item_id") or item.get("product_id"): item
            for item in payload.get("assembly_devices") or []
        }
        task.status = "done"
        task.completed_at = task.completed_at or utcnow()
        task.payload = {
            **payload,
            "replaced_by_product_tasks": True,
            "replacement_note": "Заменена отдельными задачами сборки по изделиям заказа",
        }
        for line in product_lines:
            key = line.get("order_item_id") or line.get("product_id")
            device = devices.get(key) or {}
            product_context = {
                "order_item_id": line.get("order_item_id"),
                "product_id": line.get("product_id"),
                "product_name": line.get("product_name"),
                "drawing_number": line.get("drawing_number"),
                "qty": line.get("qty"),
            }
            _ensure_assembly_task(
                db,
                order,
                bool(device.get("materials_complete", payload.get("materials_complete"))),
                product_context=product_context,
            )


def ensure_material_free_assembly_tasks(db: Session):
    orders = db.query(Order).filter(~Order.status.in_(["Ready To Ship", "Cancelled"])).all()
    for order in orders:
        assembly_tasks = db.query(WorkflowTask).filter(
            WorkflowTask.order_id == order.id,
            WorkflowTask.type == "assembler_build",
        ).all()
        existing_keys = set()
        for task in assembly_tasks:
            context = (task.payload or {}).get("product_context") or {}
            key = context.get("order_item_id") or context.get("product_id")
            if key:
                existing_keys.add(key)

        order_items = db.query(OrderItem).filter(OrderItem.order_id == order.id).all()
        for item in order_items:
            key = item.id or item.product_id
            if key in existing_keys:
                continue
            materials = get_bom_requirements(item.product_id, item.quantity, db)
            if materials:
                continue
            product = db.query(ProductType).filter(ProductType.id == item.product_id).first()
            if not product:
                continue
            _ensure_assembly_task(
                db,
                order,
                True,
                product_context={
                    "order_item_id": item.id,
                    "product_id": product.id,
                    "product_name": product.name,
                    "drawing_number": product.drawing_number,
                    "qty": item.quantity,
                },
            )


def ensure_missing_order_item_workflows(db: Session):
    orders = db.query(Order).filter(~Order.status.in_(["Ready To Ship", "Cancelled"])).all()
    for order in orders:
        tasks = db.query(WorkflowTask).filter(WorkflowTask.order_id == order.id).all()
        existing_keys = set()
        assembly_keys = set()
        for task in tasks:
            context = (task.payload or {}).get("product_context") or {}
            key = context.get("order_item_id") or context.get("product_id")
            if key:
                existing_keys.add(key)
                if task.type == "assembler_build":
                    assembly_keys.add(key)

        aggregate_shortages = []
        for item in db.query(OrderItem).filter(OrderItem.order_id == order.id).all():
            key = item.id or item.product_id
            product = db.query(ProductType).filter(ProductType.id == item.product_id).first()
            if not product:
                continue
            product_context = {
                "order_item_id": item.id,
                "product_id": product.id,
                "product_name": product.name,
                "drawing_number": product.drawing_number,
                "qty": item.quantity,
            }
            material_context = {**product_context, "product_qty": item.quantity}
            material_context.pop("qty", None)
            if key in existing_keys:
                continue
            materials = [
                {**material, **material_context}
                for material in get_bom_requirements(item.product_id, item.quantity, db)
            ]
            shortages = find_shortages(db, materials)
            create_initial_order_tasks(
                db,
                order,
                materials,
                shortages,
                product_context=product_context,
                create_procurement=False,
            )
            aggregate_shortages.extend(shortages)
        if aggregate_shortages:
            create_procurement_task_for_order(db, order, aggregate_shortages)

        # A BOM can change after an order workflow has already been created.
        # The order details calculate shortages live, while procurement stores
        # a snapshot, so keep that snapshot synchronized with the current BOM.
        stock_shortages = find_order_shortages(db, order.id)
        uncovered_shortages = _uncovered_shortages(db, order.id, stock_shortages)
        procurement = db.query(WorkflowTask).filter(
            WorkflowTask.order_id == order.id,
            WorkflowTask.type == "procurement_purchase",
            WorkflowTask.status.in_(["assigned", "open", "in_progress", "hold"]),
        ).order_by(WorkflowTask.id.asc()).first()
        if uncovered_shortages:
            if procurement:
                procurement.payload = {
                    **(procurement.payload or {}),
                    "shortages": _with_shortage_line_uids(uncovered_shortages),
                }
                if procurement.status == "hold":
                    procurement.status = "assigned"
                procurement.completed_at = None
            else:
                create_procurement_task_for_order(db, order, uncovered_shortages)
            order.status = "Procurement Required"
        elif procurement and not (procurement.payload or {}).get("purchases"):
            procurement.status = "cancelled"
            procurement.completed_at = utcnow()
            procurement.payload = {
                **(procurement.payload or {}),
                "shortages": [],
                "cancel_reason": "Текущий дефицит заказа отсутствует",
            }


def merge_order_procurement_tasks(db: Session):
    active_statuses = ["assigned", "in_progress", "open", "waiting_delivery", "hold"]
    order_ids = [
        row[0]
        for row in db.query(WorkflowTask.order_id).filter(
            WorkflowTask.type == "procurement_purchase",
            WorkflowTask.status.in_(active_statuses),
            WorkflowTask.order_id.isnot(None),
        ).distinct().all()
    ]
    for order_id in order_ids:
        tasks = db.query(WorkflowTask).filter(
            WorkflowTask.order_id == order_id,
            WorkflowTask.type == "procurement_purchase",
            WorkflowTask.status.in_(active_statuses),
        ).order_by(WorkflowTask.created_at.asc(), WorkflowTask.id.asc()).all()
        if len(tasks) <= 1:
            continue

        keeper = next((task for task in tasks if not (task.payload or {}).get("product_context")), tasks[0])
        merged_shortages = []
        merged_purchases = []
        notes = []
        for task in tasks:
            payload = task.payload or {}
            merged_shortages.extend(payload.get("shortages") or [])
            merged_purchases.extend(payload.get("purchases") or [])
            if task.id != keeper.id:
                notes.append({"task_id": task.id, "title": task.title})
                task.status = "merged"
                task.completed_at = utcnow()
                task.payload = {**payload, "merged_into_task_id": keeper.id}

        keeper.payload = {
            **(keeper.payload or {}),
            "product_context": None,
            "shortages": _deduplicate_shortage_lines(merged_shortages),
            "purchases": merged_purchases,
            "merged_tasks": [*((keeper.payload or {}).get("merged_tasks") or []), *notes],
        }
        keeper.title = f"Закупить комплектующие по заказу #{order_id}"
        keeper.description = "Оформить закупку недостающих комплектующих и передать счет бухгалтерии."
        if keeper.status == "assigned" and any(task.status in ["in_progress", "waiting_delivery"] for task in tasks):
            keeper.status = "waiting_delivery" if any(task.status == "waiting_delivery" for task in tasks) else "in_progress"


def cleanup_premature_assembly_tasks(db: Session):
    tasks = db.query(WorkflowTask).filter(
        WorkflowTask.type == "assembler_build",
        WorkflowTask.status.in_(["assigned", "open", "in_progress"]),
    ).all()
    for task in tasks:
        payload = task.payload or {}
        if payload.get("materials_complete"):
            continue
        if payload.get("daily_progress") or float(payload.get("assembled_qty") or 0) > 0:
            continue
        product_context = payload.get("product_context") or {}
        order_item_id = product_context.get("order_item_id")
        product_id = product_context.get("product_id")
        receipt_query = db.query(WorkflowTask).filter(
            WorkflowTask.order_id == task.order_id,
            WorkflowTask.type == "assembler_receive_materials",
            WorkflowTask.status == "done",
        )
        has_receipt = False
        for receipt_task in receipt_query.all():
            receipt_context = (receipt_task.payload or {}).get("product_context") or {}
            if order_item_id and receipt_context.get("order_item_id") == order_item_id:
                has_receipt = True
                break
            if not order_item_id and product_id and receipt_context.get("product_id") == product_id:
                has_receipt = True
                break
        if has_receipt:
            continue
        task.status = "cancelled"
        task.completed_at = utcnow()
        task.payload = {
            **payload,
            "cancel_reason": "Сборка создается после подтверждения получения комплектующих",
        }


def ensure_assembly_tasks_after_receipts(db: Session):
    receipt_tasks = db.query(WorkflowTask).filter(
        WorkflowTask.type == "assembler_receive_materials",
        WorkflowTask.status == "done",
        WorkflowTask.order_id.isnot(None),
    ).all()
    for receipt_task in receipt_tasks:
        payload = receipt_task.payload or {}
        product_context = payload.get("product_context") or {}
        order_item_id = product_context.get("order_item_id")
        product_id = product_context.get("product_id")
        if not product_context:
            continue
        active_assembly = False
        assembly_tasks = db.query(WorkflowTask).filter(
            WorkflowTask.order_id == receipt_task.order_id,
            WorkflowTask.type == "assembler_build",
            WorkflowTask.status.in_(["assigned", "in_progress", "open", "hold", "done"]),
        ).all()
        for assembly_task in assembly_tasks:
            assembly_context = (assembly_task.payload or {}).get("product_context") or {}
            if order_item_id and assembly_context.get("order_item_id") == order_item_id:
                active_assembly = True
                break
            if not order_item_id and product_id and assembly_context.get("product_id") == product_id:
                active_assembly = True
                break
        if active_assembly:
            continue
        order = db.query(Order).filter(Order.id == receipt_task.order_id).first()
        if order:
            _ensure_assembly_task(db, order, not bool(payload.get("partial")), product_context=product_context)


def reconcile_procurement_tasks_with_stock(db: Session):
    tasks = db.query(WorkflowTask).filter(
        WorkflowTask.type == "procurement_purchase",
        WorkflowTask.status.in_(["assigned", "open", "in_progress"]),
    ).all()
    for task in tasks:
        payload = task.payload or {}
        task.title = f"Закупить комплектующие по заказу #{task.order_id}"
        task.description = "Оформить закупку недостающих комплектующих и передать счет бухгалтерии."
        if payload.get("purchases"):
            continue
        shortages = payload.get("shortages") or []
        if not shortages:
            continue
        available_lines, remaining_shortages = plan_material_availability(db, shortages)
        if available_lines:
            for product_context, lines in _group_lines_by_product_context(available_lines):
                context_label = f" · {product_context.get('product_name')}" if product_context and product_context.get("product_name") else ""
                context_key = (
                    product_context.get("order_item_id") if product_context else None,
                    product_context.get("product_id") if product_context else None,
                )
                has_context_shortage = any(
                    (
                        (_product_context_from_line(line) or {}).get("order_item_id"),
                        (_product_context_from_line(line) or {}).get("product_id"),
                    ) == context_key
                    for line in remaining_shortages
                )
                _reserve_materials_for_issue(
                    db,
                    task.order_id,
                    lines,
                    title=f"Выдать комплектующие по заказу #{task.order_id}{context_label}",
                    description="Передать сборщику комплектующие, найденные на складе при повторной проверке.",
                    partial=has_context_shortage,
                    product_context=product_context,
                )
        if remaining_shortages:
            task.payload = {**payload, "shortages": _with_shortage_line_uids(remaining_shortages)}
        else:
            task.status = "cancelled"
            task.completed_at = utcnow()
            task.payload = {
                **payload,
                "shortages": [],
                "cancel_reason": "Дефицит закрыт свободным остатком склада",
            }


def _restore_procurement_shortage(db: Session, order_id: int, line: dict, quantity: float):
    """Return a withdrawn, not-yet-issued reservation back to procurement."""
    tasks = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order_id,
        WorkflowTask.type == "procurement_purchase",
        WorkflowTask.status.in_(["assigned", "open", "in_progress", "cancelled"]),
    ).order_by(WorkflowTask.id.desc()).all()
    task = next((
        candidate for candidate in tasks
        if not (candidate.payload or {}).get("purchases")
        and (
            candidate.status != "cancelled"
            or (candidate.payload or {}).get("cancel_reason") == "Дефицит закрыт свободным остатком склада"
        )
    ), None)

    restored = {
        **line,
        "qty": quantity,
        "shortage_qty": quantity,
        "required_qty": quantity,
        "available_qty": 0,
    }
    if task is None:
        task = create_task(
            db,
            order_id=order_id,
            task_type="procurement_purchase",
            title=f"Закупить комплектующие по заказу #{order_id}",
            role="procurement",
            description="Оформить закупку недостающих комплектующих и передать счет бухгалтерии.",
            payload={"shortages": _with_shortage_line_uids([restored])},
        )
    else:
        payload = task.payload or {}
        shortages = list(payload.get("shortages") or [])
        line_uid = restored.get("line_uid")
        match = next((
            item for item in shortages
            if (
                line_uid and item.get("line_uid") == line_uid
            ) or (
                not line_uid
                and int(item.get("component_id") or 0) == int(restored["component_id"])
                and item.get("order_item_id") == restored.get("order_item_id")
                and item.get("product_id") == restored.get("product_id")
            )
        ), None)
        if match:
            restored_qty = float(match.get("shortage_qty") or match.get("qty") or 0) + quantity
            match.update({"qty": restored_qty, "shortage_qty": restored_qty})
        else:
            shortages.append(restored)
        task.status = "assigned"
        task.completed_at = None
        task.payload = {
            **payload,
            "shortages": _with_shortage_line_uids(shortages),
            "cancel_reason": None,
        }

    order = db.query(Order).filter(Order.id == order_id).first()
    if order:
        order.status = "Procurement Required"


def reconcile_stock_shortfall(db: Session, component_id: int):
    """Withdraw unstarted issues when a manual correction makes stock insufficient."""
    stock = db.query(Stock).filter(Stock.component_id == component_id).with_for_update().first()
    if not stock:
        return
    shortfall = max(float(stock.reserved_qty or 0) - float(stock.actual_qty or 0), 0)
    if shortfall <= 0:
        return

    tasks = db.query(WorkflowTask).filter(
        WorkflowTask.type == "warehouse_issue_materials",
        WorkflowTask.status.in_(["assigned", "open"]),
    ).order_by(WorkflowTask.id.desc()).all()
    for task in tasks:
        if shortfall <= 0:
            break
        payload = task.payload or {}
        materials = []
        withdrawn = []
        for line in payload.get("materials") or []:
            if int(line.get("component_id") or 0) != int(component_id) or shortfall <= 0:
                materials.append(line)
                continue
            line_qty = float(line.get("qty") or 0)
            quantity = min(line_qty, shortfall)
            remaining = line_qty - quantity
            withdrawn.append((line, quantity))
            shortfall -= quantity
            if remaining > 0:
                materials.append({**line, "qty": remaining})

        if not withdrawn:
            continue
        task.payload = {**payload, "materials": materials}
        if not materials:
            task.status = "cancelled"
            task.completed_at = utcnow()
            task.payload = {
                **task.payload,
                "cancel_reason": "Складской остаток уменьшен до фактической выдачи",
                "hidden_from_task_lists": True,
            }

        for line, quantity in withdrawn:
            quantity_left = quantity
            reservations = db.query(Reservation).filter(
                Reservation.order_id == task.order_id,
                Reservation.component_id == component_id,
            ).order_by(Reservation.id.desc()).all()
            for reservation in reservations:
                released = min(float(reservation.qty or 0), quantity_left)
                reservation.qty = float(reservation.qty or 0) - released
                quantity_left -= released
                if reservation.qty <= 0:
                    db.delete(reservation)
                if quantity_left <= 0:
                    break
            _restore_procurement_shortage(db, task.order_id, line, quantity)

    if shortfall > 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "Нельзя уменьшить остаток ниже количества, зарезервированного "
                "в уже начатых складских выдачах"
            ),
        )
    reconcile_stock_reservations(db)


def _purchase_flow_exists(db: Session, order_id: int, purchase_id: str) -> bool:
    tasks = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order_id,
        WorkflowTask.type.in_(["accounting_payment", "warehouse_receive_components"]),
        WorkflowTask.status.in_(["assigned", "in_progress", "open", "waiting_delivery", "hold", "done"]),
    ).all()
    for task in tasks:
        if any(str(line.get("purchase_id")) == str(purchase_id) for line in (task.payload or {}).get("shortages") or []):
            return True
    return False


def ensure_procurement_payment_tasks(db: Session):
    procurement_tasks = db.query(WorkflowTask).filter(
        WorkflowTask.type == "procurement_purchase",
        WorkflowTask.status.in_(["done", "in_progress", "waiting_delivery"]),
    ).all()
    for task in procurement_tasks:
        payload = task.payload or {}
        purchases = [
            purchase for purchase in payload.get("purchases") or []
            if purchase.get("id")
            and float(purchase.get("received_qty") or 0) < float(purchase.get("qty") or 0)
            and not _purchase_flow_exists(db, task.order_id, str(purchase.get("id")))
        ]
        if not purchases:
            continue

        groups = {}
        for purchase in purchases:
            group_id = purchase.get("purchase_group_id") or purchase.get("id")
            groups.setdefault(group_id, []).append(purchase)

        for group_purchases in groups.values():
            first = group_purchases[0]
            date_label = f" на {first['expected_date']}" if first.get("expected_date") else ""
            create_task(
                db,
                order_id=task.order_id,
                task_type="accounting_payment",
                title=f"Оплатить счет по заказу #{task.order_id}{date_label}",
                role="accounting",
                description="Проверить счет закупщика, приложить оплаченное платежное поручение и передать поставку на приемку.",
                payload={
                    "procurement_task_id": task.id,
                    "purchase_group_id": first.get("purchase_group_id") or first.get("id"),
                    "shortages": [
                        {
                            **purchase,
                            "purchase_id": purchase.get("id"),
                            "qty": float(purchase.get("qty") or 0) - float(purchase.get("received_qty") or 0),
                            "shortage_qty": float(purchase.get("qty") or 0) - float(purchase.get("received_qty") or 0),
                        }
                        for purchase in group_purchases
                    ],
                    "invoice": first.get("invoice"),
                    "invoice_attachment": first.get("invoice_attachment"),
                    "expected_date": first.get("expected_date"),
                    "supplier": first.get("supplier"),
                    "comment": first.get("comment"),
                },
            )
        if not (payload.get("shortages") or []):
            task.status = "done"
            task.completed_at = task.completed_at or utcnow()
        elif task.status == "waiting_delivery":
            task.status = "in_progress"
            task.completed_at = None


def _ensure_order_reserved(db: Session, order_id: int):
    existing = db.query(Reservation).filter(Reservation.order_id == order_id).first()
    if existing:
        return

    materials = _remaining_order_materials(db, order_id)
    if not materials:
        return
    shortages = find_shortages(db, materials)
    if shortages:
        raise HTTPException(status_code=400, detail={"message": "Комплектующие все еще в дефиците", "shortages": shortages})

    reserve_components(db, materials)
    for material in materials:
        db.add(Reservation(order_id=order_id, component_id=material["component_id"], qty=material["qty"]))


def _issue_reserved_materials(db: Session, order_id: int, task: WorkflowTask | None = None,
                              actor_user_id: int | None = None, counterparty_role: str = "assembler"):
    reservations = db.query(Reservation).filter(Reservation.order_id == order_id).all()
    if not reservations:
        raise HTTPException(status_code=400, detail="По заказу нет резерва материалов")

    requested = {}
    if task:
        for line in (task.payload or {}).get("materials", []):
            component_id = int(line["component_id"])
            requested[component_id] = requested.get(component_id, 0) + float(line.get("qty") or 0)
    issued_any = False

    for item in reservations:
        if requested and item.component_id not in requested:
            continue
        issue_qty = min(float(item.qty or 0), requested.get(item.component_id, item.qty) if requested else item.qty)
        if issue_qty <= 0:
            continue
        stock = db.query(Stock).filter(Stock.component_id == item.component_id).with_for_update().first()
        if not stock:
            raise HTTPException(status_code=404, detail=f"Компонент {item.component_id} отсутствует на складе")
        stock.actual_qty = stock.actual_qty or 0
        stock.reserved_qty = stock.reserved_qty or 0
        if stock.actual_qty < issue_qty or stock.reserved_qty < issue_qty:
            raise HTTPException(status_code=400, detail=f"Недостаточно резерва компонента ID {item.component_id}")
        stock.actual_qty -= issue_qty
        stock.reserved_qty -= issue_qty
        record_movement(
            db,
            direction="outgoing",
            quantity=issue_qty,
            balance_after=stock.actual_qty,
            component_id=item.component_id,
            location=stock.location,
            task_id=task.id if task else None,
            order_id=order_id,
            actor_user_id=actor_user_id,
            counterparty_role=counterparty_role,
            note="Выдача комплектующих в сборку" if counterparty_role == "assembler" else "Выдача компонентов для ремонта",
        )
        if requested:
            requested[item.component_id] = max(float(requested.get(item.component_id, 0)) - issue_qty, 0)
        item.qty = float(item.qty or 0) - issue_qty
        if item.qty <= 0:
            db.delete(item)
        issued_any = True

    if not issued_any:
        raise HTTPException(status_code=400, detail="В резерве нет материалов из задачи")


def _receive_finished_goods(db: Session, order_id: int, task: WorkflowTask | None = None,
                            actor_user_id: int | None = None):
    if not task:
        raise HTTPException(status_code=400, detail="Оприходование возможно только из задачи приемки готовой продукции")

    payload = task.payload or {}
    completion = payload.get("completion") or {}
    finished_goods = _pending_or_legacy_product_lines(db, task, "finished_goods")
    accepted_lines = [
        line
        for line in completion.get("accepted_goods", [])
        if line.get("product_id") and float(line.get("qty") or 0) > 0
    ]
    if not accepted_lines:
        raise HTTPException(status_code=400, detail="Укажите, сколько готовой продукции принять на склад")

    accepted = {}
    for line in accepted_lines:
        product_id = int(line["product_id"])
        accepted[product_id] = accepted.get(product_id, 0) + float(line.get("qty") or 0)

    max_by_product = {}
    for line in finished_goods:
        if line.get("product_id"):
            product_id = int(line["product_id"])
            max_by_product[product_id] = max_by_product.get(product_id, 0) + float(line.get("qty") or 0)

    for product_id, qty in accepted.items():
        max_qty = max_by_product.get(product_id)
        if max_qty is not None and qty > max_qty:
            raise HTTPException(
                status_code=400,
                detail=f"Нельзя принять {qty:g} шт.: в задаче к оприходованию доступно {max_qty:g} шт.",
            )
        if qty <= 0:
            continue
        stock = db.query(Stock).filter(Stock.product_id == product_id).first()
        if stock:
            stock.actual_qty = (stock.actual_qty or 0) + qty
        else:
            stock = Stock(product_id=product_id, actual_qty=qty, location="Finished Goods")
            db.add(stock)
        packer_task = db.query(WorkflowTask).filter(
            WorkflowTask.order_id == order_id,
            WorkflowTask.type == "packer_pack",
            WorkflowTask.status == "done",
        ).order_by(WorkflowTask.id.desc()).first()
        record_movement(
            db,
            direction="incoming",
            quantity=qty,
            balance_after=stock.actual_qty,
            product_id=product_id,
            location=stock.location,
            task_id=task.id if task else None,
            order_id=order_id,
            actor_user_id=actor_user_id,
            counterparty_user_id=packer_task.assigned_user_id if packer_task else None,
            counterparty_role="packer",
            note="Оприходование готовой продукции",
        )


def _order_finished_qty_by_product(db: Session, order_id: int) -> dict[int, float]:
    result = {}
    movements = db.query(InventoryMovement).join(
        WorkflowTask,
        WorkflowTask.id == InventoryMovement.task_id,
    ).filter(
        InventoryMovement.order_id == order_id,
        InventoryMovement.direction == "incoming",
        InventoryMovement.product_id.isnot(None),
        WorkflowTask.order_id == order_id,
        WorkflowTask.type == "warehouse_finished_goods",
    ).all()
    for movement in movements:
        product_id = int(movement.product_id)
        result[product_id] = result.get(product_id, 0) + float(movement.quantity or 0)
    return result


def _order_ready_blockers(db: Session, order_id: int) -> list[str]:
    blockers = []
    active_tasks = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order_id,
        WorkflowTask.status.notin_(["done", "cancelled", "merged"]),
    ).all()
    if active_tasks:
        blockers.append("есть незавершенные задачи: " + ", ".join(f"#{task.id} {task.title}" for task in active_tasks[:5]))

    finished_qty = _order_finished_qty_by_product(db, order_id)
    for item in db.query(OrderItem).filter(OrderItem.order_id == order_id).all():
        actual_qty = finished_qty.get(int(item.product_id), 0)
        required_qty = float(item.quantity or 0)
        if actual_qty < required_qty:
            product = db.query(ProductType).filter(ProductType.id == item.product_id).first()
            product_name = product.name if product else f"Изделие ID {item.product_id}"
            blockers.append(f"{product_name}: оприходовано {actual_qty:g} из {required_qty:g} шт.")
    return blockers


def _complete_procurement_task(db: Session, task: WorkflowTask, completion_payload: dict, order: Order | None):
    payload = task.payload or {}
    shortages = [
        {
            **line,
            "requested_qty": float(
                line.get("requested_qty")
                or line.get("shortage_qty")
                or line.get("qty")
                or 0
            ),
        }
        for line in payload.get("shortages", [])
    ]
    deliveries = _delivery_lines(completion_payload)
    accepted_deliveries, remaining = _split_delivery_lines(shortages, deliveries, allow_overage=True)

    if not accepted_deliveries:
        raise HTTPException(status_code=400, detail="Укажите хотя бы одну закупленную позицию")

    if completion_payload.get("save_only"):
        task.payload = {
            **payload,
            "procurement_draft": {
                **completion_payload,
                "deliveries": accepted_deliveries,
                "save_only": False,
            },
        }
        task.status = "in_progress"
        task.completed_at = None
        if order:
            order.status = "Procurement Required"
        return {
            "status": "partial",
            "remaining": shortages,
            "message": "Черновик закупки сохранён",
        }

    purchases = list(payload.get("purchases") or [])
    for group in _group_deliveries(accepted_deliveries):
        group_items = [dict(delivery) for delivery in group["items"]]
        purchase_id = uuid.uuid4().hex
        accounting_items = []
        for item in group_items:
            purchase_line = {
                **item,
                "id": uuid.uuid4().hex,
                "purchase_group_id": purchase_id,
                "invoice_attachment": completion_payload.get("invoice_attachment"),
                "received_qty": 0,
                "created_at": utcnow().isoformat(),
            }
            purchases.append(purchase_line)
            accounting_items.append({
                **item,
                "purchase_id": purchase_line["id"],
                "qty": purchase_line["qty"],
                "shortage_qty": purchase_line["shortage_qty"],
                "invoice_attachment": completion_payload.get("invoice_attachment"),
            })
        date_label = f" на {group['expected_date']}" if group.get("expected_date") else ""
        create_task(
            db,
            order_id=task.order_id,
            task_type="accounting_payment",
            title=f"Оплатить счет по заказу #{task.order_id}{date_label}",
            role="accounting",
            description="Проверить счет закупщика, приложить оплаченное платежное поручение и передать поставку на приемку.",
            payload={
                **({
                    key: payload[key]
                    for key in (
                        "product_context",
                        "source_task_id",
                        "source_task_type",
                        "request_reason",
                        "purpose",
                    )
                    if payload.get(key) is not None
                }),
                "procurement_task_id": task.id,
                "purchase_group_id": purchase_id,
                "shortages": accounting_items,
                "invoice": group.get("invoice"),
                "invoice_attachment": completion_payload.get("invoice_attachment"),
                "expected_date": group.get("expected_date"),
                "supplier": group.get("supplier"),
                "comment": group.get("comment"),
            },
        )

    task.payload = {
        **payload,
        "shortages": remaining,
        "purchases": purchases,
        "last_completion": completion_payload,
        "procurement_draft": None,
    }

    if remaining:
        task.status = "in_progress"
        task.completed_at = None
        if order:
            order.status = "Procurement Required"
        return {"status": "partial", "remaining": remaining}

    task.status = "done"
    task.completed_at = utcnow()
    task.payload = {**task.payload, "completion": completion_payload}
    if order:
        order.status = "Awaiting Components"
    return {"status": "done"}


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
    shortages = [
        {
            **line,
            "requested_qty": float(
                line.get("requested_qty")
                or line.get("shortage_qty")
                or line.get("qty")
                or 0
            ),
        }
        for line in payload.get("shortages", [])
    ]
    purchased, remaining = _split_delivery_lines(
        shortages,
        [{"component_id": int(component_id), "qty": qty}],
        allow_overage=True,
    )
    if not purchased:
        raise HTTPException(status_code=400, detail="По этой позиции нет остатка к закупке")

    line = purchased[0]
    purchase_id = uuid.uuid4().hex
    purchase = {
        "id": purchase_id,
        **line,
        "component_id": int(component_id),
        "qty": float(line["qty"]),
        "expected_date": purchase_payload.get("expected_date"),
        "invoice": purchase_payload.get("invoice"),
        "invoice_attachment": purchase_payload.get("invoice_attachment"),
        "supplier": purchase_payload.get("supplier"),
        "comment": purchase_payload.get("comment"),
        "received_qty": 0,
        "created_at": utcnow().isoformat(),
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
        description="Проверить счет закупщика, приложить оплаченное платежное поручение и передать поставку на приемку.",
        payload={
            **({
                key: payload[key]
                for key in (
                    "product_context",
                    "source_task_id",
                    "source_task_type",
                    "request_reason",
                    "purpose",
                )
                if payload.get(key) is not None
            }),
            "procurement_task_id": task.id,
            "purchase_group_id": purchase_id,
            "shortages": [{
                **line,
                "purchase_id": purchase_id,
                "qty": purchase["qty"],
                "shortage_qty": purchase["qty"],
                "expected_date": purchase.get("expected_date"),
                "invoice": purchase.get("invoice"),
                "invoice_attachment": purchase.get("invoice_attachment"),
                "supplier": purchase.get("supplier"),
                "comment": purchase.get("comment"),
            }],
            "invoice": purchase.get("invoice"),
            "invoice_attachment": purchase.get("invoice_attachment"),
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

    task.status = "done"
    task.completed_at = utcnow()
    order = db.query(Order).filter(Order.id == task.order_id).first() if task.order_id else None
    if order:
        order.status = "Awaiting Components"
    return {"status": "done", "purchase": purchase}


def _complete_warehouse_receive_task(db: Session, task: WorkflowTask, completion_payload: dict, order: Order | None,
                                     actor_user_id: int | None = None):
    payload = task.payload or {}
    incoming = payload.get("shortages", [])
    received, remaining = _split_delivery_lines(incoming, completion_payload.get("items", []))

    if not received:
        raise HTTPException(status_code=400, detail="Укажите хотя бы одну принятую позицию")

    _receive_components_to_stock(db, received, task, actor_user_id)
    _update_procurement_purchase_receipt(db, payload, received)
    ordered_items = payload.get("ordered_items") or [dict(line) for line in incoming]
    receipt_history = [*(payload.get("receipt_history") or []), {
        "received_at": utcnow().isoformat(),
        "items": [
            {
                "component_id": int(line["component_id"]),
                "line_uid": line.get("line_uid"),
                "purchase_id": line.get("purchase_id"),
                "product_id": line.get("product_id"),
                "order_item_id": line.get("order_item_id"),
                "qty": float(line.get("qty") or line.get("shortage_qty") or 0),
            }
            for line in received
        ],
    }]
    task.payload = {
        **payload,
        "ordered_items": ordered_items,
        "receipt_history": receipt_history,
        "shortages": remaining,
        "last_completion": completion_payload,
    }

    result_status = "partial" if remaining else "done"
    if not remaining:
        task.status = "done"
        task.completed_at = utcnow()
        task.payload = {**task.payload, "completion": completion_payload}

    if payload.get("source_task_id"):
        previously_received = {}
        for receipt in payload.get("receipt_history") or []:
            for line in receipt.get("items") or []:
                key = line.get("purchase_id") or line.get("line_uid") or int(line["component_id"])
                previously_received[key] = previously_received.get(key, 0) + float(line.get("qty") or 0)
        received_for_issue = []
        for line in received:
            key = line.get("purchase_id") or line.get("line_uid") or int(line["component_id"])
            received_before = float(previously_received.get(key, 0))
            received_now = float(line.get("qty") or line.get("shortage_qty") or 0)
            requested_qty = float(line.get("requested_qty") or received_now)
            issue_qty = min(received_before + received_now, requested_qty) - min(received_before, requested_qty)
            if issue_qty > 0:
                received_for_issue.append({**line, "qty": issue_qty, "shortage_qty": issue_qty})
        product_context = payload.get("product_context") or _product_context_from_line(received[0])
        context_label = f" · {product_context.get('product_name')}" if product_context and product_context.get("product_name") else ""
        if received_for_issue:
            reserve_components(db, received_for_issue)
            for material in received_for_issue:
                db.add(Reservation(
                    order_id=task.order_id,
                    component_id=material["component_id"],
                    qty=material["qty"],
                ))
            source_role = "assembler" if payload.get("source_task_type") == "assembler_build" else "repair_engineer"
            create_task(
                db,
                order_id=task.order_id,
                task_type="repair_issue_materials",
                title=f"Выдать поступившие доп. компоненты по заказу #{task.order_id}{context_label}",
                role="warehouse",
                description="Выдать дополнительные компоненты по заявке производства; излишек закупки остается на складе.",
                payload={
                    **({"product_context": product_context} if product_context else {}),
                    "source_task_id": payload.get("source_task_id"),
                    "source_task_type": payload.get("source_task_type"),
                    "counterparty_role": source_role,
                    "materials": enrich_component_lines(db, received_for_issue),
                    "partial": bool(remaining),
                },
            )
        if order:
            order.status = (
                "Repair Required"
                if payload.get("source_task_type") == "repair_defects"
                else "Components Available"
            )
        return {"status": result_status, **({"remaining": remaining} if remaining else {})}

    grouped_received = _group_lines_by_product_context(received)
    if any(product_context for product_context, _ in grouped_received):
        for product_context, lines in grouped_received:
            context_label = f" · {product_context.get('product_name')}" if product_context and product_context.get("product_name") else ""
            _reserve_materials_for_issue(
                db,
                task.order_id,
                lines,
                title=f"Выдать поступившие комплектующие по заказу #{task.order_id}{context_label}",
                description="Передать сборщику поступившие комплектующие по этой позиции заказа.",
                partial=bool(remaining),
                product_context=product_context,
            )
        if order:
            order.status = "Components Available"
        return {"status": result_status, **({"remaining": remaining} if remaining else {})}

    if remaining:
        if order:
            order.status = "Awaiting Components"
        return {"status": "partial", "remaining": remaining}

    materials = _remaining_order_materials(db, task.order_id)
    stock_shortages = find_shortages(db, materials)
    uncovered_shortages = _uncovered_shortages(db, task.order_id, stock_shortages, exclude_task_id=task.id)
    available_materials = _available_materials(materials, stock_shortages)
    if available_materials:
        _reserve_materials_for_issue(
            db,
            task.order_id,
            available_materials,
            title=f"Выдать поступившие комплектующие по заказу #{task.order_id}",
            description="Передать сборщику поступившую часть комплектующих.",
            partial=bool(stock_shortages),
        )

    if stock_shortages:
        if order:
            order.status = "Procurement Required" if uncovered_shortages else "Awaiting Components"
        if uncovered_shortages and not _open_task_exists(db, task.order_id, "procurement_purchase"):
            create_task(
                db,
                order_id=task.order_id,
                task_type="procurement_purchase",
                title=f"Дозакупить комплектующие для заказа #{task.order_id}",
                role="procurement",
                description="Закупить оставшиеся позиции, которых все еще не хватает для запуска заказа.",
                payload={"shortages": uncovered_shortages},
            )
        return {"status": "done", "shortages": uncovered_shortages}

    _ensure_order_reserved(db, task.order_id)
    if order:
        order.status = "Components Available"
    if materials and not _open_task_exists(db, task.order_id, "warehouse_issue_materials"):
        create_task(
            db,
            order_id=task.order_id,
            task_type="warehouse_issue_materials",
            title=f"Выдать поступившие комплектующие по заказу #{task.order_id}",
            role="warehouse",
            description="Передать сборщику оставшуюся партию комплектующих.",
            payload={"materials": enrich_component_lines(db, materials)},
        )
    return {"status": "done"}


def _complete_accounting_payment_task(db: Session, task: WorkflowTask, completion_payload: dict, order: Order | None):
    payload = task.payload or {}
    procurement_task_id = payload.get("procurement_task_id")
    purchase_id = payload.get("purchase_id")
    if procurement_task_id and purchase_id:
        procurement_task = db.query(WorkflowTask).filter(
            WorkflowTask.id == int(procurement_task_id),
            WorkflowTask.type == "procurement_purchase",
        ).first()
        if procurement_task:
            procurement_payload = procurement_task.payload or {}
            purchases = []
            for purchase in procurement_payload.get("purchases") or []:
                if str(purchase.get("id")) == str(purchase_id):
                    purchase = {
                        **purchase,
                        "payment_ref": completion_payload.get("payment_ref") or purchase.get("payment_ref"),
                        "payment_order_attachment": completion_payload.get("payment_order_attachment") or purchase.get("payment_order_attachment"),
                        "payment_comment": completion_payload.get("notes") or purchase.get("payment_comment"),
                        "paid_at": purchase.get("paid_at") or utcnow().isoformat(),
                    }
                purchases.append(purchase)
            procurement_task.payload = {**procurement_payload, "purchases": purchases}
            if procurement_task.status != "done":
                procurement_task.status = "waiting_delivery"
                procurement_task.completed_at = None
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


def create_initial_order_tasks(db: Session, order: Order, materials: list[dict], shortages: list[dict],
                               product_context: dict | None = None, create_procurement: bool = True,
                               available_materials: list[dict] | None = None):
    available_materials = available_materials if available_materials is not None else _available_materials(materials, shortages)
    context_label = f" · {product_context['product_name']}" if product_context and product_context.get("product_name") else ""
    payload_context = {"product_context": product_context} if product_context else {}
    if not materials and not shortages:
        if order:
            order.status = "In Assembly"
            _ensure_assembly_task(db, order, True, product_context=product_context)
        return
    if available_materials:
        reserve_components(db, available_materials)
        for material in available_materials:
            db.add(Reservation(
                order_id=order.id,
                component_id=material["component_id"],
                qty=material["qty"],
            ))
        create_task(
            db,
            order_id=order.id,
            task_type="warehouse_issue_materials",
            title=f"Выдать комплектующие по заказу #{order.id}{context_label}",
            role="warehouse",
            description="Передать сборщику доступную часть комплекта; недостающие позиции закупаются параллельно.",
            payload={**payload_context, "materials": enrich_component_lines(db, available_materials), "partial": bool(shortages)},
        )

    if shortages:
        order.status = "Procurement Required"
        if create_procurement:
            create_task(
                db,
                order_id=order.id,
                task_type="procurement_purchase",
                title=f"Закупить комплектующие по заказу #{order.id}{context_label}",
                role="procurement",
                description="Оформить закупку недостающих комплектующих и передать счет бухгалтерии.",
                payload={**payload_context, "shortages": _with_shortage_line_uids(shortages)},
            )
    else:
        order.status = "Reserved"


def _complete_assembler_build_task(
    db: Session,
    task: WorkflowTask,
    completion_payload: dict,
    order: Order | None,
    actor_user_id: int | None = None,
):
    payload = normalize_task_daily_progress(task)
    if payload.get("preassembly_test_required") and not payload.get("preassembly_test_completed"):
        raise HTTPException(
            status_code=409,
            detail="Сначала завершите предварительное тестирование устройств до сборки в корпус",
        )
    product_context = payload.get("product_context")
    target_qty = float(product_context.get("qty") or 0) if product_context else _order_target_qty(db, task.order_id)
    materials_complete = bool(payload.get("materials_complete")) if product_context else _assembly_materials_complete(db, task.order_id)
    save_only = bool(completion_payload.get("save_only"))
    today = utcnow().date().isoformat()
    units = (
        db.query(Item)
        .filter(Item.assembly_task_id == task.id)
        .order_by(Item.id.asc())
        .all()
    )
    serial_selection_mode = isinstance(completion_payload.get("assembled_serial_numbers"), list)
    selected_serials = {
        str(serial_number)
        for serial_number in (completion_payload.get("assembled_serial_numbers") or [])
        if str(serial_number).strip()
    }
    if serial_selection_mode:
        task_serials = {unit.serial_number for unit in units}
        unknown_serials = selected_serials - task_serials
        if unknown_serials:
            raise HTTPException(
                status_code=422,
                detail=f"Заводские номера не относятся к этой задаче: {', '.join(sorted(unknown_serials))}",
            )
        transferred_serials = set(payload.get("transferred_serial_numbers") or [])
        if transferred_serials - selected_serials:
            raise HTTPException(
                status_code=422,
                detail="Нельзя снять отметку с устройства, которое уже передано на тестирование",
            )
        if "assembly_claims" in payload:
            assembly_claims = {
                str(serial_number): int(user_id)
                for serial_number, user_id in (payload.get("assembly_claims") or {}).items()
            }
            newly_assembled_serials = {
                unit.serial_number
                for unit in units
                if unit.serial_number in selected_serials and unit.status in ["planned", "in_assembly"]
            }
            foreign_or_unclaimed = [
                serial_number
                for serial_number in newly_assembled_serials
                if assembly_claims.get(serial_number) != actor_user_id
            ]
            if foreign_or_unclaimed:
                raise HTTPException(
                    status_code=409,
                    detail="Можно отмечать собранными только устройства, закреплённые за вами",
                )
            payload = {
                **payload,
                "assembly_claims": {
                    serial_number: user_id
                    for serial_number, user_id in assembly_claims.items()
                    if serial_number not in newly_assembled_serials
                },
            }
            task.payload = payload

    assignments = _default_assembly_assignments(task, payload, target_qty)
    if completion_payload.get("assembly_assignments"):
        assignments = _merge_assembly_assignments(assignments, completion_payload.get("assembly_assignments") or [])
    planned_total = sum(float(item.get("planned_qty") or 0) for item in assignments)
    if target_qty > 0 and planned_total > target_qty:
        raise HTTPException(
            status_code=400,
            detail=f"Нельзя назначить в сборку {planned_total:g} шт.: в заказе запланировано {target_qty:g} шт.",
        )

    daily_entries = []
    legacy_daily_qty = completion_payload.get("daily_qty")
    if not serial_selection_mode and legacy_daily_qty not in [None, ""]:
        daily_entries.append({
            "assignment_id": assignments[0]["id"] if assignments else str(uuid.uuid4()),
            "user_id": task.assigned_user_id,
            "qty": legacy_daily_qty,
            "comment": completion_payload.get("daily_comment"),
            "transfer_from_user_id": completion_payload.get("transfer_from_user_id"),
        })
    if not serial_selection_mode:
        daily_entries.extend(completion_payload.get("daily_entries") or [])

    daily_progress = list(payload.get("daily_progress") or [])
    assignment_by_id = {item["id"]: item for item in assignments if item.get("id")}
    saved_entries = []
    for entry in daily_entries:
        qty = float(entry.get("qty") or 0)
        if qty < 0:
            raise HTTPException(status_code=400, detail="Количество за день не может быть отрицательным")
        if qty <= 0:
            continue
        assignment_id = entry.get("assignment_id")
        assignment = assignment_by_id.get(assignment_id) or (assignments[0] if assignments else None)
        if not assignment:
            continue
        assignment["produced_qty"] = float(assignment.get("produced_qty") or 0) + qty
        planned_qty = float(assignment.get("planned_qty") or 0)
        overage_qty = max(float(assignment.get("produced_qty") or 0) - planned_qty, 0)
        if overage_qty > 0 and not entry.get("transfer_from_user_id"):
            raise HTTPException(status_code=400, detail="Сборщик превысил свой план. Укажите, у кого забран пул устройств")
        saved_entry = {
            "date": today,
            "assignment_id": assignment["id"],
            "product_id": assignment.get("product_id"),
            "product_name": assignment.get("product_name"),
            "drawing_number": assignment.get("drawing_number"),
            "user_id": entry.get("user_id") or assignment.get("user_id") or task.assigned_user_id,
            "user_name": _user_name(db, entry.get("user_id") or assignment.get("user_id") or task.assigned_user_id),
            "qty": qty,
            "comment": entry.get("comment") or "",
            "transfer_from_user_id": entry.get("transfer_from_user_id"),
            "transfer_from_user_name": _user_name(db, entry.get("transfer_from_user_id")),
            "overage_qty": overage_qty,
        }
        saved_entries.append(saved_entry)
        daily_progress.append(saved_entry)

    if serial_selection_mode and assignments:
        if len(assignments) == 1:
            assignments[0]["produced_qty"] = float(len(selected_serials))
        else:
            produced_by_user = {}
            for unit in units:
                if unit.serial_number in selected_serials and unit.assigned_user_id:
                    produced_by_user[unit.assigned_user_id] = produced_by_user.get(unit.assigned_user_id, 0) + 1
            for assignment in assignments:
                assignment["produced_qty"] = float(produced_by_user.get(assignment.get("user_id"), 0))

    assembled_qty = float(len(selected_serials)) if serial_selection_mode else float(completion_payload.get("assembled_qty") or 0)
    produced_total = sum(float(item.get("produced_qty") or 0) for item in assignments)
    if assembled_qty <= 0:
        assembled_qty = produced_total
    if not saved_entries and assembled_qty <= 0 and not completion_payload.get("assembly_assignments"):
        raise HTTPException(status_code=400, detail="Укажите дневную отметку, план сборщика или фактически собранное количество")
    product_lines = payload.get("product_lines") or _order_product_lines(db, task.order_id)
    lines_by_key = {
        str(line.get("order_item_id") or line.get("product_id")): line
        for line in product_lines
    }
    planned_by_device = {}
    produced_by_device = {}
    for assignment in assignments:
        key = str(assignment.get("order_item_id") or assignment.get("product_id") or "")
        if not key:
            continue
        planned_by_device[key] = planned_by_device.get(key, 0) + float(assignment.get("planned_qty") or 0)
        produced_by_device[key] = produced_by_device.get(key, 0) + float(assignment.get("produced_qty") or 0)

    if product_context:
        product_lines = [{
            "order_item_id": product_context.get("order_item_id"),
            "product_id": product_context.get("product_id"),
            "product_name": product_context.get("product_name"),
            "drawing_number": product_context.get("drawing_number"),
            "qty": product_context.get("qty") or target_qty,
        }]
        lines_by_key = {str(product_context.get("order_item_id") or product_context.get("product_id")): product_lines[0]}

    for key, line in lines_by_key.items():
        line_name = line.get("product_name") or f"изделие ID {line.get('product_id')}"
        line_qty = float(line.get("qty") or 0)
        line_context = {
            "order_item_id": line.get("order_item_id"),
            "product_id": line.get("product_id"),
            "product_name": line.get("product_name"),
            "drawing_number": line.get("drawing_number"),
            "qty": line_qty,
        }
        planned_qty = planned_by_device.get(key, 0)
        produced_qty = produced_by_device.get(key, 0)
        if line_qty > 0 and planned_qty > line_qty:
            raise HTTPException(
                status_code=400,
                detail=f"Нельзя назначить {planned_qty:g} шт. для {line_name}: в заказе {line_qty:g} шт.",
            )
        if line_qty > 0 and produced_qty > line_qty:
            raise HTTPException(
                status_code=400,
                detail=f"Нельзя собрать {produced_qty:g} шт. для {line_name}: в заказе требуется {line_qty:g} шт.",
            )
        max_buildable_qty = _max_buildable_qty_from_issued(db, task.order_id, line_context)
        if produced_qty > max_buildable_qty:
            raise HTTPException(
                status_code=400,
                detail=f"Нельзя собрать {produced_qty:g} шт. для {line_name}: комплектующих выдано максимум на {max_buildable_qty:g} шт.",
            )

    stored_completion = {
        key: value
        for key, value in completion_payload.items()
        if key not in ["daily_entries", "daily_qty", "daily_comment", "assembled_qty", "extra_components"]
    }
    task.payload = {
        **payload,
        "product_lines": product_lines,
        "planned_qty": target_qty,
        "materials_complete": materials_complete,
        "started_qty": max(assembled_qty, produced_total, float(payload.get("started_qty") or 0)),
        "assembly_assignments": assignments,
        "daily_progress": daily_progress,
        "completion": stored_completion,
    }
    assembled_units_count = min(int(produced_total), len(units))
    for index, unit in enumerate(units):
        should_be_assembled = unit.serial_number in selected_serials if serial_selection_mode else index < assembled_units_count
        if should_be_assembled:
            if unit.status in ["planned", "in_assembly", "assembled"]:
                unit.status = "assembled"
                unit.assembly_started_at = unit.assembly_started_at or utcnow()
                unit.assembled_at = unit.assembled_at or utcnow()
        elif serial_selection_mode and unit.status == "assembled":
            unit.status = "in_assembly"
            unit.assembled_at = None
        elif produced_total > 0 and unit.status == "planned":
            unit.status = "in_assembly"
            unit.assembly_started_at = unit.assembly_started_at or utcnow()
    if order:
        order.status = "In Assembly"

    material_flow = _create_repair_material_flow(db, task, completion_payload.get("extra_components") or [])
    if material_flow.get("created"):
        saved_completion = task.payload.get("completion") or {}
        task.payload = {
            **task.payload,
            "completion": {**saved_completion, "extra_components": []},
        }
        task.status = "in_progress"
        task.completed_at = None
        if order:
            order.status = "In Assembly"
        return {
            "status": "partial",
            "message": "Созданы задачи на выдачу или закупку дополнительных компонентов для сборки",
            "materials": material_flow,
        }

    if save_only:
        task.status = "in_progress"
        task.completed_at = None
        return {
            "status": "partial",
            "started_qty": task.payload.get("started_qty") or 0,
            "planned_qty": target_qty,
            "message": "Отметка сборки сохранена",
        }

    if _open_repair_material_flow_exists(db, task.id):
        raise HTTPException(status_code=400, detail="Сначала нужно закрыть выдачу, закупку и приемку дополнительных компонентов для сборки")

    if assembled_qty <= 0:
        raise HTTPException(status_code=400, detail="Укажите фактически собранное количество")

    transferred_to_test_qty = float(payload.get("transferred_to_test_qty") or 0)
    transfer_qty = max(assembled_qty - transferred_to_test_qty, 0)
    if transfer_qty <= 0:
        tester_exists = (
            _tester_task_exists_for_context(db, task.order_id, product_context)
            if product_context
            else bool(db.query(WorkflowTask).filter(
                WorkflowTask.order_id == task.order_id,
                WorkflowTask.type == "tester_check",
                WorkflowTask.status.in_(["assigned", "open", "in_progress", "hold", "done"]),
            ).first())
        )
        if assembled_qty > 0 and not tester_exists:
            transfer_qty = assembled_qty
        else:
            raise HTTPException(status_code=400, detail="Нет нового выпуска для передачи на тестирование")

    assembly_finished = materials_complete and assembled_qty >= target_qty
    task.payload = {**task.payload, "transferred_to_test_qty": assembled_qty}
    task.status = "done" if assembly_finished else "in_progress"
    task.completed_at = utcnow() if assembly_finished else None
    if order:
        order.status = "Quality Check" if assembly_finished else "In Assembly"
    context_label = f" · {product_context.get('product_name')}" if product_context and product_context.get("product_name") else ""
    product_lines_for_test = []
    units_for_test = [
        unit for unit in units
        if unit.status == "assembled"
        and unit.serial_number not in set(payload.get("transferred_serial_numbers") or [])
    ][:int(transfer_qty)]
    for key, line in lines_by_key.items():
        qty = produced_by_device.get(key, 0)
        if qty <= 0 and product_context:
            qty = assembled_qty
        if product_context:
            qty = transfer_qty
        if qty <= 0:
            continue
        line_checklist = _product_test_checklist(db, line.get("product_id"))
        product_lines_for_test.append({
            "order_item_id": line.get("order_item_id"),
            "product_id": line.get("product_id"),
            "product_name": line.get("product_name"),
            "drawing_number": line.get("drawing_number"),
            "qty": int(qty),
            "test_checklist": line_checklist,
        })
    if not product_lines_for_test:
        product_lines_for_test = [
            {**line, "test_checklist": _product_test_checklist(db, line.get("product_id"))}
            for line in product_lines
        ]
    task_checklist = product_lines_for_test[0].get("test_checklist") if len(product_lines_for_test) == 1 else []
    transferred_serial_numbers = [unit.serial_number for unit in units_for_test]
    for unit in units_for_test:
        unit.status = "testing"
    task.payload = {
        **task.payload,
        "transferred_serial_numbers": [
            *(payload.get("transferred_serial_numbers") or []),
            *transferred_serial_numbers,
        ],
    }
    create_task(
        db,
        order_id=task.order_id,
        task_type="tester_check",
        title=f"Протестировать изделия по заказу #{task.order_id}{context_label}",
        role="tester",
        description="Отметить годные и бракованные изделия.",
        payload={
            **({"product_context": product_context} if product_context else {}),
            "source_assembly_task_id": task.id,
            "pre_assembly": False,
            "retest": False,
            "assembled_qty": assembled_qty,
            "planned_qty": target_qty,
            "product_lines": product_lines_for_test,
            "test_checklist": task_checklist,
            "unit_ids": [unit.id for unit in units_for_test],
            "serial_numbers": transferred_serial_numbers,
        },
    )
    return {
        "status": "done" if assembly_finished else "partial",
        "transferred_qty": transfer_qty,
        "message": "Выпуск передан на тестирование" if assembly_finished else "Часть выпуска передана на тестирование, сборка остается в работе",
    }


def _complete_task_impl(db: Session, task: WorkflowTask, completion_payload: dict | None = None,
                        actor_user_id: int | None = None):
    if task.status == "done":
        raise HTTPException(status_code=400, detail="Задача уже закрыта")

    payload = task.payload or {}
    completion_payload = completion_payload or {}
    order = db.query(Order).filter(Order.id == task.order_id).first() if task.order_id else None

    if task.type == "procurement_purchase":
        return _complete_procurement_task(db, task, completion_payload, order)

    elif task.type == "order_adjustment_return":
        incoming = (task.payload or {}).get("materials") or []
        returned, remaining = _split_delivery_lines(incoming, completion_payload.get("items", []))
        if not returned:
            raise HTTPException(status_code=400, detail="Укажите хотя бы одну возвращённую позицию")
        for line in returned:
            component_id = int(line["component_id"])
            quantity = float(line.get("qty") or line.get("shortage_qty") or 0)
            stock = db.query(Stock).filter(Stock.component_id == component_id).with_for_update().first()
            if not stock:
                stock = Stock(component_id=component_id, actual_qty=0, reserved_qty=0, location="Warehouse-1")
                db.add(stock)
                db.flush()
            stock.actual_qty = float(stock.actual_qty or 0) + quantity
            record_movement(
                db,
                direction="incoming",
                quantity=quantity,
                balance_after=stock.actual_qty,
                component_id=component_id,
                location=stock.location,
                task_id=task.id,
                order_id=task.order_id,
                actor_user_id=actor_user_id,
                note="Возврат лишних комплектующих после изменения количества заказа",
            )
        task.payload = {
            **(task.payload or {}),
            "materials": remaining,
            "return_history": [
                *((task.payload or {}).get("return_history") or []),
                {"created_at": utcnow().isoformat(), "items": returned},
            ],
            "last_completion": completion_payload,
        }
        task.status = "in_progress" if remaining else "done"
        task.completed_at = None if remaining else utcnow()
        return {"status": "partial" if remaining else "done", "remaining": remaining}

    elif task.type == "warehouse_receive_components":
        return _complete_warehouse_receive_task(db, task, completion_payload, order, actor_user_id)

    elif task.type == "accounting_payment":
        task.payload = {**payload, "completion": completion_payload}
        task.status = "done"
        task.completed_at = utcnow()
        return _complete_accounting_payment_task(db, task, completion_payload, order)

    elif task.type == "assembler_build":
        return _complete_assembler_build_task(db, task, completion_payload, order, actor_user_id)

    if task.type == "warehouse_issue_materials":
        task.payload = {**payload, "completion": completion_payload}
        _issue_reserved_materials(db, task.order_id, task, actor_user_id, counterparty_role="assembler")
        transfer = _mark_material_transfer_issued(db, task, "assembler", actor_user_id)
        db.flush()
        product_context = payload.get("product_context")
        remaining_materials = []
        if product_context:
            if order:
                order.status = "Materials Issued"
            partial = bool(payload.get("partial"))
            context_label = f" · {product_context.get('product_name')}" if product_context.get("product_name") else ""
        else:
            remaining_materials = _remaining_order_materials(db, task.order_id)
            stock_shortages = find_shortages(db, remaining_materials) if remaining_materials else []
            remaining_shortages = _uncovered_shortages(db, task.order_id, stock_shortages) if stock_shortages else []
            if order:
                if remaining_shortages:
                    order.status = "Procurement Required"
                elif stock_shortages:
                    order.status = "Awaiting Components"
                else:
                    order.status = "Materials Issued"
            partial = bool(remaining_materials)
            context_label = ""
        task.status = "ready_to_issue"
        task.completed_at = None
        receive_task = create_task(
            db,
            order_id=task.order_id,
            task_type="assembler_receive_materials",
            title=f"Получить {'часть ' if partial else ''}комплектующих по заказу #{task.order_id}{context_label}",
            role="assembler",
            description="Подтвердить получение выданной складом партии комплектующих.",
            payload={
                **({"product_context": product_context} if product_context else {}),
                "source_issue_task_id": task.id,
                "materials": payload.get("materials", []),
                "partial": partial,
            },
        )
        _attach_material_transfer_receipt(db, transfer, receive_task)
        return {"status": "ready_to_issue"}

    task.payload = {**payload, "completion": completion_payload}
    task.status = "done"
    task.completed_at = utcnow()

    if task.type == "assembler_receive_materials":
        _accept_material_transfer(db, task, actor_user_id)
        source_issue_task_id = payload.get("source_issue_task_id")
        movement_query = db.query(InventoryMovement).filter(
            InventoryMovement.order_id == task.order_id,
            InventoryMovement.direction == "outgoing",
            InventoryMovement.counterparty_role == "assembler",
            InventoryMovement.counterparty_user_id.is_(None),
        )
        if source_issue_task_id:
            movement_query = movement_query.filter(InventoryMovement.task_id == int(source_issue_task_id))
        for movement in movement_query.all():
            movement.counterparty_user_id = actor_user_id
        if source_issue_task_id:
            issue_task = db.query(WorkflowTask).filter(
                WorkflowTask.id == int(source_issue_task_id),
                WorkflowTask.type.in_(["warehouse_issue_materials", "repair_issue_materials"]),
                WorkflowTask.status == "ready_to_issue",
            ).first()
            if issue_task:
                issue_task.status = "done"
                issue_task.completed_at = utcnow()
        product_context = payload.get("product_context")
        if product_context:
            materials_complete = not bool(payload.get("partial"))
            if order:
                order.status = "In Assembly"
                _ensure_assembly_task(db, order, materials_complete, product_context=product_context)
        else:
            remaining_materials = _remaining_order_materials(db, task.order_id)
            stock_shortages = find_shortages(db, remaining_materials) if remaining_materials else []
            remaining_shortages = _uncovered_shortages(db, task.order_id, stock_shortages) if stock_shortages else []
            if remaining_materials and not _open_task_exists(db, task.order_id, "warehouse_issue_materials"):
                available_remaining = _available_materials(
                    remaining_materials,
                    stock_shortages,
                )
                if available_remaining:
                    reserve_components(db, available_remaining)
                    for material in available_remaining:
                        db.add(Reservation(
                            order_id=task.order_id,
                            component_id=material["component_id"],
                            qty=material["qty"],
                        ))
                    create_task(
                        db,
                        order_id=task.order_id,
                        task_type="warehouse_issue_materials",
                        title=f"Выдать следующую партию комплектующих по заказу #{task.order_id}",
                        role="warehouse",
                        description="Передать сборщику поступившую часть комплекта.",
                        payload={"materials": enrich_component_lines(db, available_remaining), "partial": True},
                    )
            materials_complete = _assembly_materials_complete(db, task.order_id, exclude_receipt_id=task.id)
            if not materials_complete:
                if order:
                    if remaining_shortages:
                        order.status = "Procurement Required"
                    elif stock_shortages:
                        order.status = "Awaiting Components"
                    else:
                        order.status = "Materials Issued"
            else:
                if order:
                    order.status = "In Assembly"
            if order:
                if materials_complete:
                    order.status = "In Assembly"
                _ensure_assembly_task(db, order, materials_complete)

    elif task.type == "repair_issue_materials":
        recipient_role = payload.get("counterparty_role") or "repair_engineer"
        _issue_reserved_materials(
            db,
            task.order_id,
            task,
            actor_user_id,
            counterparty_role=recipient_role,
        )
        transfer = _mark_material_transfer_issued(db, task, recipient_role, actor_user_id)
        db.flush()
        task.status = "ready_to_issue"
        task.completed_at = None
        product_context = payload.get("product_context")
        context_label = f" · {product_context.get('product_name')}" if product_context and product_context.get("product_name") else ""
        is_assembly_request = payload.get("source_task_type") == "assembler_build"
        receive_task = create_task(
            db,
            order_id=task.order_id,
            task_type="assembler_receive_materials" if is_assembly_request else "repair_receive_materials",
            title=(
                f"Получить дополнительные компоненты для сборки по заказу #{task.order_id}{context_label}"
                if is_assembly_request
                else f"Получить дополнительные компоненты для ремонта по заказу #{task.order_id}{context_label}"
            ),
            role="assembler" if is_assembly_request else "repair_engineer",
            description=(
                "Подтвердить получение дополнительных компонентов от склада перед продолжением сборки."
                if is_assembly_request
                else "Подтвердить получение дополнительных компонентов от склада перед продолжением ремонта."
            ),
            payload={
                **({"product_context": product_context} if product_context else {}),
                "source_task_id": payload.get("source_task_id"),
                "source_task_type": payload.get("source_task_type"),
                "source_issue_task_id": task.id,
                "request_reason": payload.get("request_reason"),
                "materials": payload.get("materials", []),
            },
        )
        _attach_material_transfer_receipt(db, transfer, receive_task)
        if order:
            order.status = "In Assembly" if is_assembly_request else "Repair Required"
        return {"status": "ready_to_issue", "receive_task_id": receive_task.id}

    elif task.type == "repair_receive_materials":
        _accept_material_transfer(db, task, actor_user_id)
        source_issue_task_id = payload.get("source_issue_task_id")
        movement_query = db.query(InventoryMovement).filter(
            InventoryMovement.order_id == task.order_id,
            InventoryMovement.direction == "outgoing",
            InventoryMovement.counterparty_role == "repair_engineer",
            InventoryMovement.counterparty_user_id.is_(None),
        )
        if source_issue_task_id:
            movement_query = movement_query.filter(InventoryMovement.task_id == int(source_issue_task_id))
        for movement in movement_query.all():
            movement.counterparty_user_id = actor_user_id
        if source_issue_task_id:
            issue_task = db.query(WorkflowTask).filter(
                WorkflowTask.id == int(source_issue_task_id),
                WorkflowTask.type == "repair_issue_materials",
                WorkflowTask.status == "ready_to_issue",
            ).first()
            if issue_task:
                issue_task.status = "done"
                issue_task.completed_at = utcnow()
        if order:
            order.status = "Repair Required"

    elif task.type == "tester_check":
        product_context = payload.get("product_context")
        product_lines = _pending_or_legacy_product_lines(
            db, task, "product_lines", aggregate_all=True
        )
        if not product_lines and batch_summary(db, task)["batches_total"] == 0:
            product_lines = _order_product_lines(db, task.order_id)
        test_total_qty = sum(float(item.get("qty") or 0) for item in product_lines)
        available_units = (
            db.query(Item)
            .filter(
                Item.id.in_(payload.get("unit_ids") or [-1]),
                Item.status == "testing",
            )
            .order_by(Item.id.asc())
            .all()
        )
        serial_test_results = completion_payload.get("serial_test_results")
        results_by_serial = {}
        if available_units and not isinstance(serial_test_results, list):
            raise HTTPException(
                status_code=422,
                detail="Проверьте каждое изделие по заводскому номеру",
            )
        if isinstance(serial_test_results, list):
            results_by_serial = {
                str(item.get("serial_number")): item
                for item in serial_test_results
                if item.get("serial_number") and item.get("reviewed")
            }
            available_serials = {unit.serial_number for unit in available_units}
            if not results_by_serial:
                raise HTTPException(
                    status_code=422,
                    detail="Зафиксируйте результат хотя бы одного устройства",
                )
            if set(results_by_serial) - available_serials:
                raise HTTPException(
                    status_code=422,
                    detail="В результатах есть заводской номер, которого нет в текущей партии",
                )
            if "testing_claims" in payload:
                testing_claims = {
                    str(serial_number): int(user_id)
                    for serial_number, user_id in (payload.get("testing_claims") or {}).items()
                }
                foreign_or_unclaimed = [
                    serial_number
                    for serial_number in results_by_serial
                    if testing_claims.get(serial_number) != actor_user_id
                ]
                if foreign_or_unclaimed:
                    raise HTTPException(
                        status_code=409,
                        detail="Можно сохранять результаты только по устройствам, закреплённым за вами",
                    )
                task.payload = {
                    **(task.payload or {}),
                    "testing_claims": {
                        serial_number: user_id
                        for serial_number, user_id in testing_claims.items()
                        if serial_number not in results_by_serial
                    },
                }
                payload = task.payload
            available_units = [
                unit for unit in available_units
                if unit.serial_number in results_by_serial
            ]
            defective_serials_from_checklists = {
                serial_number
                for serial_number, result in results_by_serial.items()
                if any(not bool(item.get("checked")) for item in (result.get("checklist") or []))
            }
            product_metadata = {
                int(line["product_id"]): line
                for line in product_lines
                if line.get("product_id")
            }
            selected_by_product = {}
            defective_by_product = {}
            for unit in available_units:
                product_id = int(unit.product_id or (product_context or {}).get("product_id"))
                selected_by_product[product_id] = selected_by_product.get(product_id, 0) + 1
                if unit.serial_number in defective_serials_from_checklists:
                    defective_by_product[product_id] = defective_by_product.get(product_id, 0) + 1
            product_lines = [
                {
                    **product_metadata.get(product_id, {"product_id": product_id}),
                    "qty": quantity,
                }
                for product_id, quantity in selected_by_product.items()
            ]
            test_total_qty = len(available_units)
            defective_products_from_checklists = [
                {
                    **product_metadata.get(product_id, {"product_id": product_id}),
                    "defective_qty": quantity,
                }
                for product_id, quantity in defective_by_product.items()
            ]
            completion_payload = {
                **completion_payload,
                "serial_test_results": list(results_by_serial.values()),
                "defective_serial_numbers": sorted(defective_serials_from_checklists),
                "defective_qty": len(defective_serials_from_checklists),
                "passed_qty": len(available_units) - len(defective_serials_from_checklists),
                "defective_products": defective_products_from_checklists,
            }
        defective_products = [
            {
                "product_id": int(item["product_id"]),
                "product_name": item.get("product_name"),
                "drawing_number": item.get("drawing_number"),
                "defective_qty": int(item.get("defective_qty") or 0),
            }
            for item in completion_payload.get("defective_products") or []
            if item.get("product_id") and int(item.get("defective_qty") or 0) > 0
        ]
        defective_qty = sum(item["defective_qty"] for item in defective_products) or int(completion_payload.get("defective_qty") or 0)
        passed_qty = float(completion_payload.get("passed_qty") or 0)
        if test_total_qty <= 0:
            raise HTTPException(status_code=400, detail="В задаче тестирования нет изделий для проверки")
        if passed_qty + defective_qty <= 0:
            raise HTTPException(status_code=400, detail="Укажите количество годных и/или бракованных изделий")
        tested_qty = passed_qty + defective_qty
        if abs(tested_qty - test_total_qty) > 0.000001:
            raise HTTPException(
                status_code=400,
                detail=f"Тестирование должно закрыть ровно {test_total_qty:g} шт.: указано {tested_qty:g} шт.",
            )
        checklist = completion_payload.get("test_checklist") or payload.get("test_checklist") or []
        if not isinstance(serial_test_results, list) and checklist and any(not item.get("checked") for item in checklist):
            raise HTTPException(status_code=400, detail="Нельзя закрыть тестирование: чеклист проверки заполнен не полностью")
        requested_defective_serials = set(completion_payload.get("defective_serial_numbers") or [])
        available_serials = {unit.serial_number for unit in available_units}
        if requested_defective_serials - available_serials:
            raise HTTPException(status_code=422, detail="Выбран заводской номер, которого нет в текущей партии тестирования")
        if requested_defective_serials and len(requested_defective_serials) != int(defective_qty):
            raise HTTPException(status_code=422, detail="Количество отмеченных заводских номеров с браком не совпадает с количеством брака")
        if not requested_defective_serials and defective_qty > 0:
            requested_defective_serials = {unit.serial_number for unit in available_units[:int(defective_qty)]}
        passed_units = []
        defective_units = []
        for unit in available_units:
            unit.tested_at = utcnow()
            if unit.serial_number in requested_defective_serials:
                unit.status = "repair"
                unit.test_result = "defective"
                unit.defect_note = completion_payload.get("notes")
                defective_units.append(unit)
            else:
                unit.status = "passed"
                unit.test_result = "passed"
                passed_units.append(unit)
        pre_assembly_test = bool(payload.get("pre_assembly"))
        source_assembly_task = None
        if pre_assembly_test and payload.get("source_assembly_task_id"):
            source_assembly_task = db.query(WorkflowTask).filter(
                WorkflowTask.id == int(payload["source_assembly_task_id"]),
                WorkflowTask.type == "assembler_build",
            ).first()
        if source_assembly_task and passed_units:
            source_payload = source_assembly_task.payload or {}
            passed_serials = {
                *(source_payload.get("preassembly_passed_serial_numbers") or []),
                *[unit.serial_number for unit in passed_units],
            }
            for unit in passed_units:
                unit.status = "planned"
                unit.assigned_user_id = None
            source_assembly_task.payload = {
                **source_payload,
                "preassembly_passed_serial_numbers": sorted(passed_serials),
                "blocked_reason": None,
            }
            source_assembly_task.status = "assigned"
            source_assembly_task.completed_at = None
            if order:
                order.status = "In Assembly"
        if defective_qty > 0:
            if order:
                order.status = "Repair Required"
            defective_product_ids = [item["product_id"] for item in defective_products]
            serial_defects = [
                {
                    "serial_number": unit.serial_number,
                    "product_id": unit.product_id,
                    "failed_checks": [
                        {
                            "id": item.get("id"),
                            "label": item.get("label") or str(item.get("id") or "Неисправность"),
                        }
                        for item in (results_by_serial.get(unit.serial_number, {}).get("checklist") or [])
                        if not bool(item.get("checked"))
                    ],
                    "tester_note": completion_payload.get("notes") or "",
                }
                for unit in defective_units
            ]
            context_label = f" · {product_context.get('product_name')}" if product_context and product_context.get("product_name") else ""
            defective_by_product = {
                int(item["product_id"]): int(item.get("defective_qty") or 0)
                for item in defective_products
            }
            passed_product_lines = []
            remaining_legacy_defects = defective_qty if not defective_products else 0
            for line in product_lines:
                line_qty = float(line.get("qty") or 0)
                product_id = int(line["product_id"]) if line.get("product_id") else None
                line_defective = defective_by_product.get(product_id, 0) if product_id else 0
                if remaining_legacy_defects > 0:
                    line_defective = min(line_qty, remaining_legacy_defects)
                    remaining_legacy_defects -= line_defective
                line_passed = max(line_qty - line_defective, 0)
                if line_passed > 0:
                    passed_product_lines.append({**line, "qty": line_passed})
            if passed_product_lines and not pre_assembly_test:
                create_task(
                    db,
                    order_id=task.order_id,
                    task_type="packer_pack",
                    title=f"Упаковать годные изделия по заказу #{task.order_id}{context_label}",
                    role="packer",
                    description="Упаковать изделия, успешно прошедшие тестирование; бракованная часть партии передана в ремонт.",
                    payload={
                        **({"product_context": product_context} if product_context else {}),
                        "source_test_task_id": task.id,
                        "product_lines": passed_product_lines,
                        "unit_ids": [unit.id for unit in passed_units],
                        "serial_numbers": [unit.serial_number for unit in passed_units],
                    },
                )
            create_task(
                db,
                order_id=task.order_id,
                task_type="repair_defects",
                title=f"Устранить брак по заказу #{task.order_id}{context_label}",
                role="repair_engineer",
                description="Устранить выявленные дефекты и передать изделия на повторное тестирование.",
                payload={
                    **({"product_context": product_context} if product_context else {}),
                    "source_test_task_id": task.id,
                    "source_assembly_task_id": payload.get("source_assembly_task_id"),
                    "pre_assembly": pre_assembly_test,
                    "defective_qty": defective_qty,
                    "defective_products": defective_products,
                    "notes": completion_payload.get("notes"),
                    "component_options": _order_bom_component_options(db, task.order_id, product_ids=defective_product_ids or None),
                    "unit_ids": [unit.id for unit in defective_units],
                    "serial_numbers": [unit.serial_number for unit in defective_units],
                    "serial_defects": serial_defects,
                },
            )
        elif not pre_assembly_test:
            if order:
                order.status = "Ready For Packing"
            context_label = f" · {product_context.get('product_name')}" if product_context and product_context.get("product_name") else ""
            create_task(
                db,
                order_id=task.order_id,
                task_type="packer_pack",
                title=f"Упаковать изделия по заказу #{task.order_id}{context_label}",
                role="packer",
                description="Упаковать годные изделия и передать на склад готовой продукции.",
                payload={
                    **({"product_context": product_context} if product_context else {}),
                    "product_lines": product_lines,
                    "unit_ids": [unit.id for unit in passed_units],
                    "serial_numbers": [unit.serial_number for unit in passed_units],
                },
            )
        elif source_assembly_task:
            all_source_units = db.query(Item).filter(
                Item.assembly_task_id == source_assembly_task.id,
            ).all()
            if all(unit.status not in ["testing", "repair"] for unit in all_source_units):
                source_assembly_task.payload = {
                    **(source_assembly_task.payload or {}),
                    "preassembly_test_completed": True,
                    "blocked_reason": None,
                }
        consume_workflow_batches(db, task, tested_qty)
        upstream_open = False if pre_assembly_test else (
            _active_primary_testing_exists(db, task.order_id, exclude_task_id=task.id)
            if payload.get("retest")
            else _active_stage_exists_for_context(
                db,
                task.order_id,
                ["assembler_build"],
                product_context,
                exclude_task_id=task.id,
            )
        )
        remaining = batch_summary(db, task)["pending_qty"]
        if upstream_open or remaining > 0:
            history = list(payload.get("completion_history") or [])
            history.append({**completion_payload, "processed_qty": tested_qty, "completed_at": utcnow().isoformat()})
            task.payload = {**task.payload, "completion": {}, "completion_history": history}
            task.status = "assigned"
            task.completed_at = None

    elif task.type == "repair_defects":
        material_flow = _create_repair_material_flow(db, task, completion_payload.get("extra_components") or [])
        if material_flow.get("created"):
            task.status = "in_progress"
            task.completed_at = None
            task.payload = {
                **(task.payload or {}),
                "completion": {
                    **completion_payload,
                    "extra_components": [],
                },
                "last_completion": completion_payload,
            }
            if order:
                order.status = "Repair Required"
            return {
                "status": "partial",
                "message": "Созданы задачи на выдачу или закупку дополнительных компонентов для ремонта",
                "materials": material_flow,
            }
        if _open_repair_material_flow_exists(db, task.id):
            raise HTTPException(status_code=400, detail="Сначала нужно закрыть выдачу, закупку и приемку дополнительных компонентов для ремонта")
        product_context = payload.get("product_context")
        context_label = f" · {product_context.get('product_name')}" if product_context and product_context.get("product_name") else ""
        repair_pending_lines = pending_product_lines(db, task)
        repaired_units = (
            db.query(Item)
            .filter(
                Item.id.in_(payload.get("unit_ids") or [-1]),
                Item.status == "repair",
            )
            .order_by(Item.id.asc())
            .all()
        )
        repair_results = completion_payload.get("serial_repair_results")
        if repaired_units:
            if not isinstance(repair_results, list):
                raise HTTPException(
                    status_code=422,
                    detail="Опишите выполненный ремонт для каждого заводского номера",
                )
            repair_results_by_serial = {
                str(item.get("serial_number")): item
                for item in repair_results
                if item.get("serial_number") and str(item.get("work_done") or "").strip()
            }
            repaired_serials = {unit.serial_number for unit in repaired_units}
            if set(repair_results_by_serial) != repaired_serials:
                raise HTTPException(
                    status_code=422,
                    detail="Для каждого ремонтируемого устройства нужно указать, что было сделано",
                )
            repair_results = [
                {
                    **repair_results_by_serial[unit.serial_number],
                    "work_done": str(repair_results_by_serial[unit.serial_number]["work_done"]).strip(),
                }
                for unit in repaired_units
            ]
            for unit in repaired_units:
                unit.defect_note = repair_results_by_serial[unit.serial_number]["work_done"].strip()
            completion_payload = {
                **completion_payload,
                "serial_repair_results": repair_results,
            }
        else:
            repair_results = []
        if repaired_units:
            product_metadata = {
                int(item["product_id"]): item
                for item in (
                    repair_pending_lines
                    or payload.get("defective_products")
                    or payload.get("product_lines")
                    or [product_context]
                )
                if item and item.get("product_id")
            }
            repaired_by_product = {}
            for unit in repaired_units:
                product_id = int(unit.product_id or (product_context or {}).get("product_id"))
                repaired_by_product[product_id] = repaired_by_product.get(product_id, 0) + 1
            repaired_product_lines = [
                {
                    **product_metadata.get(product_id, {"product_id": product_id}),
                    "qty": quantity,
                    "test_checklist": _product_test_checklist(db, product_id),
                }
                for product_id, quantity in repaired_by_product.items()
            ]
        else:
            repaired_product_lines = [
                {
                    "product_id": item.get("product_id"),
                    "product_name": item.get("product_name"),
                    "drawing_number": item.get("drawing_number"),
                    "qty": int(item.get("qty") or item.get("defective_qty") or 0),
                    "test_checklist": _product_test_checklist(db, item.get("product_id")),
                }
                for item in (repair_pending_lines or payload.get("defective_products") or [])
                if item.get("product_id") and int(item.get("qty") or item.get("defective_qty") or 0) > 0
            ] or [
                {
                    **item,
                    "test_checklist": _product_test_checklist(db, item.get("product_id")),
                }
                for item in (payload.get("product_lines") or _order_product_lines(db, task.order_id))
            ]
        task_checklist = repaired_product_lines[0].get("test_checklist") if len(repaired_product_lines) == 1 else []
        for unit in repaired_units:
            unit.status = "testing"
            unit.test_result = None
        if order:
            order.status = "Quality Check"
        create_task(
            db,
            order_id=task.order_id,
            task_type="tester_check",
            title=f"Повторно протестировать изделия после ремонта по заказу #{task.order_id}{context_label}",
            role="tester",
            description="Проверить отремонтированные изделия. На упаковку передаются только изделия, успешно прошедшие повторное тестирование.",
            payload={
                **({"product_context": product_context} if product_context else {}),
                "source_repair_task_id": task.id,
                "retest": True,
                "source_assembly_task_id": payload.get("source_assembly_task_id"),
                "pre_assembly": bool(payload.get("pre_assembly")),
                "repair_notes": completion_payload.get("notes"),
                "repair_results": repair_results,
                "product_lines": repaired_product_lines,
                "test_checklist": task_checklist,
                "unit_ids": [unit.id for unit in repaired_units],
                "serial_numbers": [unit.serial_number for unit in repaired_units],
            },
        )
        repaired_qty = sum(float(line.get("qty") or 0) for line in repaired_product_lines)
        consume_workflow_batches(db, task, repaired_qty)
        if batch_summary(db, task)["pending_qty"] > 0:
            history = list(payload.get("completion_history") or [])
            history.append({**completion_payload, "processed_qty": repaired_qty, "completed_at": utcnow().isoformat()})
            task.payload = {**task.payload, "completion": {}, "completion_history": history}
            task.status = "assigned"
            task.completed_at = None

    elif task.type == "packer_pack":
        product_lines = _pending_or_legacy_product_lines(
            db,
            task,
            "product_lines",
            aggregate_all=True,
        )
        if not product_lines and batch_summary(db, task)["batches_total"] == 0:
            product_lines = _order_product_lines(db, task.order_id)
        available_qty = sum(float(item.get("qty") or 0) for item in product_lines)
        serial_selection_mode = isinstance(completion_payload.get("packed_serial_numbers"), list)
        selected_serials = {
            str(serial_number)
            for serial_number in (completion_payload.get("packed_serial_numbers") or [])
            if str(serial_number).strip()
        }
        available_units = (
            db.query(Item)
            .filter(
                Item.id.in_(payload.get("unit_ids") or [-1]),
                Item.status == "passed",
            )
            .order_by(Item.id.asc())
            .all()
        )
        if serial_selection_mode:
            available_serials = {unit.serial_number for unit in available_units}
            unknown_serials = selected_serials - available_serials
            if unknown_serials:
                raise HTTPException(
                    status_code=422,
                    detail=f"Нельзя упаковать недоступные устройства: {', '.join(sorted(unknown_serials))}",
                )
            packed_units = [unit for unit in available_units if unit.serial_number in selected_serials]
            packed_qty = float(len(packed_units))
        else:
            packed_qty = float(completion_payload.get("packed_qty") or 0)
            packed_units = available_units[:int(packed_qty)]
        if available_qty <= 0:
            raise HTTPException(status_code=400, detail="В задаче упаковки нет изделий для упаковки")
        if packed_qty <= 0:
            raise HTTPException(status_code=400, detail="Отметьте хотя бы одно упакованное устройство")
        if packed_qty > available_qty:
            raise HTTPException(
                status_code=400,
                detail=f"Нельзя упаковать {packed_qty:g} шт.: доступно {available_qty:g} шт.",
            )
        packed_by_product = {}
        for unit in packed_units:
            packed_by_product[unit.product_id] = packed_by_product.get(unit.product_id, 0) + 1
        selected_product_lines = []
        for line in product_lines:
            product_id = line.get("product_id")
            line_qty = min(float(line.get("qty") or 0), float(packed_by_product.get(product_id, 0)))
            if line_qty <= 0:
                continue
            selected_product_lines.append({**line, "qty": line_qty})
            packed_by_product[product_id] -= line_qty
        product_lines = selected_product_lines or _take_product_quantity(product_lines, packed_qty)
        for unit in packed_units:
            unit.status = "packed"
            unit.packed_at = utcnow()
        if order:
            order.status = "Finished Goods"
        product_context = payload.get("product_context")
        context_label = f" · {product_context.get('product_name')}" if product_context and product_context.get("product_name") else ""
        create_task(
            db,
            order_id=task.order_id,
            task_type="warehouse_finished_goods",
            title=f"Оприходовать готовую продукцию по заказу #{task.order_id}{context_label}",
            role="warehouse",
            description="Поставить готовые изделия на баланс склада готовой продукции.",
            payload={
                **({"product_context": product_context} if product_context else {}),
                "source_pack_task_id": task.id,
                "packed_qty": packed_qty,
                "finished_goods": product_lines,
                "unit_ids": [unit.id for unit in packed_units],
                "serial_numbers": [unit.serial_number for unit in packed_units],
            },
        )
        consume_workflow_batches(db, task, packed_qty)
        remaining = batch_summary(db, task)["pending_qty"]
        if remaining > 0 or _active_stage_exists_for_context(
            db,
            task.order_id,
            ["tester_check", "repair_defects"],
            product_context,
            exclude_task_id=task.id,
        ):
            history = list(payload.get("completion_history") or [])
            history.append({**completion_payload, "processed_qty": packed_qty, "completed_at": utcnow().isoformat()})
            task.payload = {**task.payload, "completion": {}, "completion_history": history}
            task.status = "assigned"
            task.completed_at = None

    elif task.type == "warehouse_finished_goods":
        accepted_by_product = {}
        for line in completion_payload.get("accepted_goods") or []:
            if not line.get("product_id"):
                continue
            product_id = int(line["product_id"])
            accepted_by_product[product_id] = (
                accepted_by_product.get(product_id, 0)
                + float(line.get("qty") or 0)
            )
        tracked_unit_ids = payload.get("unit_ids") or []
        available_units = (
            db.query(Item)
            .filter(
                Item.id.in_(tracked_unit_ids or [-1]),
                Item.status == "packed",
            )
            .order_by(Item.id.asc())
            .all()
        )
        available_by_product = {}
        for unit in available_units:
            available_by_product[unit.product_id] = available_by_product.get(unit.product_id, 0) + 1
        if tracked_unit_ids:
            for product_id, accepted_qty in accepted_by_product.items():
                available_qty = available_by_product.get(product_id, 0)
                if accepted_qty > available_qty:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Нельзя принять {accepted_qty:g} шт.: "
                            f"к оприходованию доступно {available_qty:g} шт."
                        ),
                    )
            if not available_units:
                raise HTTPException(status_code=400, detail="Эта партия уже оприходована")

        _receive_finished_goods(db, task.order_id, task, actor_user_id)
        stocked_units = []
        remaining_by_product = dict(accepted_by_product)
        for unit in available_units:
            if remaining_by_product.get(unit.product_id, 0) <= 0:
                continue
            stocked_units.append(unit)
            remaining_by_product[unit.product_id] -= 1
        accepted_qty = (
            float(len(stocked_units))
            if tracked_unit_ids
            else sum(accepted_by_product.values())
        )
        for unit in stocked_units:
            unit.status = "stocked"
            unit.stocked_at = utcnow()
        consume_workflow_batches(db, task, accepted_qty)
        db.flush()
        remaining = batch_summary(db, task)["pending_qty"]
        product_context = payload.get("product_context")
        if remaining > 0 or _active_stage_exists_for_context(
            db,
            task.order_id,
            ["packer_pack"],
            product_context,
            exclude_task_id=task.id,
        ):
            history = list(payload.get("completion_history") or [])
            history.append({**completion_payload, "processed_qty": accepted_qty, "completed_at": utcnow().isoformat()})
            task.payload = {**task.payload, "completion": {}, "completion_history": history}
            task.status = "assigned"
            task.completed_at = None
        if order:
            blockers = _order_ready_blockers(db, task.order_id)
            if blockers:
                order.status = "Finished Goods"
                return {
                    "status": "done",
                    "order_status": order.status,
                    "order_blockers": blockers,
                }
            else:
                order.status = "Ready To Ship"

    return {"status": "done"}


def complete_task(db: Session, task: WorkflowTask, completion_payload: dict | None = None,
                  actor_user_id: int | None = None):
    """Единая точка перехода: маршрут, количества и партии фиксируются централизованно."""
    completion_payload = completion_payload or {}
    sync_task_quantities(db, task)
    existing_ids = {
        row[0] for row in db.query(WorkflowTask.id).filter(WorkflowTask.order_id == task.order_id).all()
    }
    result = _complete_task_impl(db, task, completion_payload, actor_user_id)
    db.flush()
    new_tasks = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == task.order_id,
        ~WorkflowTask.id.in_(existing_ids) if existing_ids else WorkflowTask.id.isnot(None),
    ).all()
    for new_task in new_tasks:
        new_payload = new_task.payload or {}
        explicit_source_id = next((
            new_payload.get(key)
            for key in (
                "source_test_task_id",
                "source_repair_task_id",
                "source_issue_task_id",
                "source_pack_task_id",
                "source_assembly_task_id",
            )
            if new_payload.get(key)
        ), None)
        transition_source = (
            db.query(WorkflowTask).filter(WorkflowTask.id == int(explicit_source_id)).first()
            if explicit_source_id else None
        ) or task
        validate_transition(transition_source.type, new_task.type)
        sync_task_quantities(db, new_task)
    sync_task_quantities(db, task)
    record_completion_batch(db, task, completion_payload)
    if task.type in ACCUMULATIVE_TYPES and task.status != "done":
        result = {
            **(result or {}),
            "status": "partial",
            "message": "Партия обработана; накопительная задача остается открытой для следующих поступлений",
            "batch_summary": batch_summary(db, task),
        }
    return result
