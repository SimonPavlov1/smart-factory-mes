import uuid

from sqlalchemy.orm import Session

from app.models.production import WorkflowBatch, WorkflowBatchLine, WorkflowTask
from app.time_utils import utcnow


ACCUMULATIVE_TYPES = {
    "tester_check": ("product_lines", "qty"),
    "repair_defects": ("defective_products", "defective_qty"),
    "packer_pack": ("product_lines", "qty"),
    "warehouse_finished_goods": ("finished_goods", "qty"),
}
ACTIVE_STATUSES = {"assigned", "open", "in_progress", "hold"}
REOPENABLE_TYPES = {"tester_check", "packer_pack", "warehouse_finished_goods"}


def workflow_cycle(task_type: str, payload: dict) -> str:
    if task_type == "tester_check" and payload.get("retest"):
        return "retest"
    return "primary"


def _context_key(payload: dict):
    context = payload.get("product_context") or {}
    return context.get("order_item_id"), context.get("product_id")


def find_accumulative_task(db: Session, order_id: int, task_type: str, payload: dict):
    if task_type not in ACCUMULATIVE_TYPES:
        return None
    order_item_id, product_id = _context_key(payload)
    cycle = workflow_cycle(task_type, payload)
    statuses = [*ACTIVE_STATUSES]
    if task_type in REOPENABLE_TYPES:
        statuses.append("done")
    candidates = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order_id,
        WorkflowTask.type == task_type,
        WorkflowTask.status.in_(statuses),
    ).order_by(WorkflowTask.id.desc()).all()
    active_candidates = [task for task in candidates if task.status in ACTIVE_STATUSES]
    completed_candidates = [task for task in candidates if task.status == "done"]
    for task in [*active_candidates, *completed_candidates]:
        existing = task.payload or {}
        if task_type != "tester_check" and workflow_cycle(task_type, existing) != cycle:
            continue
        existing_order_item_id, existing_product_id = _context_key(existing)
        if order_item_id and existing_order_item_id == order_item_id:
            return task
        if not order_item_id and product_id and existing_product_id == product_id:
            return task
        if not order_item_id and not product_id and not existing_order_item_id and not existing_product_id:
            return task
    return None


def _merge_lines(current: list[dict], incoming: list[dict], qty_key: str) -> list[dict]:
    result = [dict(line) for line in current]
    indexes = {}
    for index, line in enumerate(result):
        key = line.get("order_item_id") or line.get("product_id") or line.get("component_id")
        if key:
            indexes[key] = index
    for line in incoming:
        key = line.get("order_item_id") or line.get("product_id") or line.get("component_id")
        if key in indexes:
            index = indexes[key]
            result[index][qty_key] = float(result[index].get(qty_key) or 0) + float(line.get(qty_key) or 0)
        else:
            indexes[key] = len(result)
            result.append(dict(line))
    return result


def merge_accumulative_payload(task: WorkflowTask, incoming: dict):
    collection, qty_key = ACCUMULATIVE_TYPES[task.type]
    current = task.payload or {}
    merged = {**current}
    merged[collection] = _merge_lines(current.get(collection) or [], incoming.get(collection) or [], qty_key)
    for list_key in ("unit_ids", "serial_numbers"):
        merged[list_key] = list(dict.fromkeys([
            *(current.get(list_key) or []),
            *(incoming.get(list_key) or []),
        ]))
    for scalar_key in (
        "pre_assembly",
        "source_assembly_task_id",
        "source_test_task_id",
        "source_repair_task_id",
        "source_pack_task_id",
        "product_context",
        "test_checklist",
        "planned_qty",
        "assembled_qty",
    ):
        if scalar_key in incoming:
            merged[scalar_key] = incoming[scalar_key]
    if task.type == "tester_check" and incoming.get("retest"):
        merged["retest_serial_numbers"] = list(dict.fromkeys([
            *(current.get("retest_serial_numbers") or []),
            *(incoming.get("serial_numbers") or []),
        ]))
        repair_results = {
            item.get("serial_number"): dict(item)
            for item in (current.get("repair_results") or [])
            if item.get("serial_number")
        }
        for item in incoming.get("repair_results") or []:
            if item.get("serial_number"):
                repair_results[item["serial_number"]] = dict(item)
        merged["repair_results"] = list(repair_results.values())
    if task.type == "repair_defects":
        serial_defects = {
            item.get("serial_number"): dict(item)
            for item in (current.get("serial_defects") or [])
            if item.get("serial_number")
        }
        for item in incoming.get("serial_defects") or []:
            if item.get("serial_number"):
                serial_defects[item["serial_number"]] = dict(item)
        merged["serial_defects"] = list(serial_defects.values())
    source_ids = list(current.get("source_task_ids") or [])
    for key in ("source_assembly_task_id", "source_test_task_id", "source_repair_task_id", "source_pack_task_id"):
        source_id = incoming.get(key)
        if source_id and source_id not in source_ids:
            source_ids.append(source_id)
    merged["source_task_ids"] = source_ids
    if task.type == "repair_defects":
        merged["defective_qty"] = sum(float(line.get("defective_qty") or 0) for line in merged[collection])
    task.payload = merged
    return merged


def record_workflow_batch(db: Session, task: WorkflowTask, incoming: dict):
    collection, qty_key = ACCUMULATIVE_TYPES[task.type]
    source_id = next((
        incoming.get(key) for key in
        ("source_assembly_task_id", "source_test_task_id", "source_repair_task_id", "source_pack_task_id")
        if incoming.get(key)
    ), None)
    batch_key = str(incoming.get("workflow_batch_key") or uuid.uuid4())
    batch = WorkflowBatch(
        order_id=task.order_id, container_task_id=task.id, source_task_id=source_id,
        stage=task.type, cycle=workflow_cycle(task.type, incoming), batch_key=batch_key,
    )
    db.add(batch)
    db.flush()
    for line in incoming.get(collection) or []:
        entity_id = line.get("product_id") or line.get("component_id")
        quantity = float(line.get(qty_key) or 0)
        if entity_id and quantity > 0:
            batch.lines.append(WorkflowBatchLine(
                entity_type="product" if line.get("product_id") else "component",
                entity_id=int(entity_id), quantity=quantity,
            ))
    db.flush()
    return batch


def consume_workflow_batches(db: Session, task: WorkflowTask, quantity: float):
    remaining = float(quantity or 0)
    batches = db.query(WorkflowBatch).filter(
        WorkflowBatch.container_task_id == task.id,
        WorkflowBatch.status.in_(["queued", "processing"]),
    ).order_by(WorkflowBatch.id.asc()).all()
    for batch in batches:
        for line in batch.lines:
            available = max(float(line.quantity or 0) - float(line.processed_qty or 0), 0)
            consumed = min(available, remaining)
            line.processed_qty = float(line.processed_qty or 0) + consumed
            remaining -= consumed
            if remaining <= 0:
                break
        if all(float(line.processed_qty or 0) >= float(line.quantity or 0) for line in batch.lines):
            batch.status = "processed"
            batch.processed_at = utcnow()
        else:
            batch.status = "processing"
        if remaining <= 0:
            break


def batch_summary(db: Session, task: WorkflowTask) -> dict:
    batches = db.query(WorkflowBatch).filter(WorkflowBatch.container_task_id == task.id).all()
    received = sum(float(line.quantity or 0) for batch in batches for line in batch.lines)
    processed = sum(float(line.processed_qty or 0) for batch in batches for line in batch.lines)
    current_batch = next((
        batch for batch in batches
        if any(float(line.processed_qty or 0) < float(line.quantity or 0) for line in batch.lines)
    ), None)
    current_qty = sum(
        max(float(line.quantity or 0) - float(line.processed_qty or 0), 0)
        for line in (current_batch.lines if current_batch else [])
    )
    return {
        "batches_total": len(batches),
        "batches_waiting": sum(batch.status != "processed" for batch in batches),
        "received_qty": received,
        "processed_qty": processed,
        "pending_qty": max(received - processed, 0),
        "current_batch_id": current_batch.id if current_batch else None,
        "current_batch_qty": current_qty,
    }


def pending_product_lines(db: Session, task: WorkflowTask, *, aggregate_all: bool = False) -> list[dict]:
    payload_lines = (task.payload or {}).get(
        ACCUMULATIVE_TYPES.get(task.type, ("product_lines", "qty"))[0]
    ) or []
    metadata = {
        int(line["product_id"]): line for line in payload_lines if line.get("product_id")
    }
    pending = {}
    batches = db.query(WorkflowBatch).filter(
        WorkflowBatch.container_task_id == task.id,
        WorkflowBatch.status.in_(["queued", "processing"]),
    ).order_by(WorkflowBatch.id.asc()).all()
    batches_to_include = batches if aggregate_all else [next((
        batch for batch in batches
        if any(float(line.processed_qty or 0) < float(line.quantity or 0) for line in batch.lines)
    ), None)]
    for batch in batches_to_include:
        if not batch:
            continue
        for line in batch.lines:
            if line.entity_type != "product":
                continue
            quantity = max(float(line.quantity or 0) - float(line.processed_qty or 0), 0)
            if quantity > 0:
                pending[int(line.entity_id)] = pending.get(int(line.entity_id), 0) + quantity
    return [
        {**metadata.get(product_id, {"product_id": product_id}), "qty": quantity}
        for product_id, quantity in pending.items()
    ]
