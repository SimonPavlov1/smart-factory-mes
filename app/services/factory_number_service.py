import re

from sqlalchemy.orm import Session

from app.models.production import FactoryNumberSequence, Item, ProductType


def factory_number_prefix(product: ProductType) -> str:
    source = product.drawing_number or product.sku or product.name or f"PRODUCT-{product.id}"
    normalized = re.sub(r"[^A-Za-zА-Яа-я0-9.]+", "-", source.upper()).strip("-.")
    return normalized[:48] or f"PRODUCT-{product.id}"


def create_product_units(
    db: Session,
    *,
    order_id: int,
    order_item_id: int | None,
    product: ProductType,
    assembly_task_id: int,
    assigned_user_id: int | None,
    quantity: int,
) -> list[Item]:
    if quantity <= 0:
        return []
    prefix = factory_number_prefix(product)
    sequence_scope = 0
    sequence = (
        db.query(FactoryNumberSequence)
        .filter_by(prefix=prefix, year=sequence_scope)
        .with_for_update()
        .first()
    )
    configured_start = max(1, product.factory_number_start or 1)
    if not sequence:
        sequence = FactoryNumberSequence(
            prefix=prefix,
            year=sequence_scope,
            last_value=configured_start - 1,
        )
        db.add(sequence)
        db.flush()

    start = max(sequence.last_value + 1, configured_start)
    sequence.last_value = start + quantity - 1
    units = []
    for number in range(start, start + quantity):
        unit = Item(
            order_id=order_id,
            order_item_id=order_item_id,
            product_id=product.id,
            assembly_task_id=assembly_task_id,
            assigned_user_id=assigned_user_id,
            serial_number=f"{prefix}-{number:03d}",
            status="planned",
        )
        db.add(unit)
        units.append(unit)
    db.flush()
    return units
