"""Единая модель количеств и нормализованные партии workflow."""
import uuid

from sqlalchemy.orm import Session

from app.models.production import MaterialBatch, MaterialBatchLine, MaterialTransfer, TaskQuantity, WorkflowTask


LINE_COLLECTIONS = (
    ("shortages", "component", "requested_qty"),
    ("materials", "component", "reserved_qty"),
    ("purchases", "component", "purchased_qty"),
    ("ordered_items", "component", "purchased_qty"),
    ("product_lines", "product", "processed_qty"),
    ("finished_goods", "product", "processed_qty"),
)


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def _line_quantity(line: dict, semantic: str) -> float:
    keys = {
        "requested_qty": ("requested_qty", "shortage_qty", "qty"),
        "reserved_qty": ("reserved_qty", "qty"),
        "purchased_qty": ("purchased_qty", "ordered_qty", "qty"),
        "received_qty": ("received_qty", "accepted_qty", "qty"),
        "processed_qty": ("processed_qty", "packed_qty", "qty"),
    }[semantic]
    return next((_number(line.get(key)) for key in keys if line.get(key) is not None), 0)


def sync_task_quantities(db: Session, task: WorkflowTask):
    payload = task.payload or {}
    existing = {
        (row.entity_type, row.entity_id, row.line_uid): row
        for row in db.query(TaskQuantity).filter(TaskQuantity.task_id == task.id).all()
    }
    for collection, entity_type, semantic in LINE_COLLECTIONS:
        for index, line in enumerate(payload.get(collection) or []):
            entity_id = line.get("component_id" if entity_type == "component" else "product_id")
            if not entity_id:
                continue
            line_uid = str(line.get("line_uid") or f"{collection}:{index}:{entity_id}")
            key = (entity_type, int(entity_id), line_uid)
            row = existing.get(key)
            if not row:
                row = TaskQuantity(
                    task_id=task.id, order_id=task.order_id, entity_type=entity_type,
                    entity_id=int(entity_id), line_uid=line_uid,
                )
                db.add(row)
                existing[key] = row
            setattr(row, semantic, max(getattr(row, semantic) or 0, _line_quantity(line, semantic)))
            row.received_qty = max(row.received_qty or 0, _number(line.get("received_qty")))
            row.accepted_qty = max(row.accepted_qty or 0, _number(line.get("accepted_qty")))
            row.rejected_qty = max(row.rejected_qty or 0, _number(line.get("rejected_qty") or line.get("defective_qty")))
    transfers = db.query(MaterialTransfer).filter(
        (MaterialTransfer.issue_task_id == task.id) | (MaterialTransfer.receive_task_id == task.id)
    ).all()
    for transfer in transfers:
        for line in transfer.lines:
            line_uid = str(line.line_uid or f"transfer:{transfer.id}:{line.component_id}")
            key = ("component", int(line.component_id), line_uid)
            row = existing.get(key)
            if not row:
                row = TaskQuantity(
                    task_id=task.id, order_id=task.order_id, entity_type="component",
                    entity_id=int(line.component_id), line_uid=line_uid,
                )
                db.add(row)
                existing[key] = row
            row.requested_qty = max(row.requested_qty or 0, line.requested_qty or 0)
            row.reserved_qty = max(row.reserved_qty or 0, line.reserved_qty or 0)
            row.issued_qty = max(row.issued_qty or 0, line.issued_qty or 0)
            row.accepted_qty = max(row.accepted_qty or 0, line.accepted_qty or 0)
    db.flush()


def record_completion_batch(db: Session, task: WorkflowTask, completion: dict):
    batch_types = {
        "procurement_purchase": "purchase",
        "warehouse_receive_components": "receipt",
        "tester_check": "test",
        "packer_pack": "packing",
        "warehouse_finished_goods": "finished_goods",
    }
    batch_type = batch_types.get(task.type)
    if not batch_type:
        return None
    external_key = str(completion.get("batch_id") or completion.get("purchase_id") or "completion")
    batch = db.query(MaterialBatch).filter_by(source_task_id=task.id, external_key=external_key).first()
    if batch:
        return batch
    batch = MaterialBatch(
        order_id=task.order_id, source_task_id=task.id, batch_type=batch_type,
        external_key=external_key, document_ref=completion.get("invoice_number") or completion.get("document_number"),
    )
    db.add(batch)
    db.flush()
    payload = task.payload or {}
    lines = (
        completion.get("items") or completion.get("received_items") or completion.get("purchases")
        or payload.get("finished_goods") or payload.get("product_lines") or payload.get("materials") or []
    )
    scalar_qty = _number(
        completion.get("packed_qty") or completion.get("accepted_goods")
        or completion.get("passed_qty")
    )
    if not lines and scalar_qty:
        lines = payload.get("product_lines") or payload.get("finished_goods") or []
    for index, line in enumerate(lines):
        entity_type = "component" if line.get("component_id") else "product"
        entity_id = line.get("component_id") or line.get("product_id")
        if not entity_id:
            continue
        qty = _number(
            line.get("received_qty") or line.get("purchased_qty") or line.get("ordered_qty")
            or line.get("accepted_qty") or line.get("qty")
        )
        if scalar_qty and len(lines) == 1:
            qty = scalar_qty
        if qty <= 0:
            continue
        batch.lines.append(MaterialBatchLine(
            entity_type=entity_type, entity_id=int(entity_id),
            line_uid=str(line.get("line_uid") or f"{index}:{entity_id}"),
            quantity=qty, rejected_qty=_number(line.get("rejected_qty") or line.get("defective_qty")),
        ))
    db.flush()
    return batch
