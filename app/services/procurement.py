from datetime import date
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.inventory import Stock
from app.models.procurement import PurchaseItem, PurchaseOrder


def create_purchase_order(
    db: Session,
    supplier_name: str,
    items: Iterable[dict],
) -> PurchaseOrder:
    purchase_order = PurchaseOrder(supplier_name=supplier_name, status="Draft")
    db.add(purchase_order)
    db.flush()

    for item in items:
        db.add(
            PurchaseItem(
                order_id=purchase_order.id,
                component_id=item["component_id"],
                qty=item["qty"],
                price=item.get("price"),
            )
        )

    db.commit()
    db.refresh(purchase_order)
    return purchase_order


def get_purchase_order(db: Session, purchase_id: int) -> PurchaseOrder | None:
    return db.get(PurchaseOrder, purchase_id)


def list_purchase_orders(
    db: Session,
    status: str | None = None,
    supplier_name: str | None = None,
) -> list[PurchaseOrder]:
    stmt = select(PurchaseOrder)

    if status:
        stmt = stmt.where(PurchaseOrder.status == status)

    if supplier_name:
        stmt = stmt.where(PurchaseOrder.supplier_name == supplier_name)

    stmt = stmt.order_by(PurchaseOrder.id.desc())
    return list(db.scalars(stmt).all())


def list_purchase_items(db: Session, purchase_id: int) -> list[PurchaseItem]:
    stmt = (
        select(PurchaseItem)
        .where(PurchaseItem.order_id == purchase_id)
        .order_by(PurchaseItem.id.asc())
    )
    return list(db.scalars(stmt).all())


def update_purchase_delivery(
    db: Session,
    purchase_id: int,
    tracking_code: str | None = None,
    arrival_date: date | None = None,
    invoice_ref: str | None = None,
    status: str | None = "In Transit",
) -> PurchaseOrder | None:
    purchase_order = db.get(PurchaseOrder, purchase_id)
    if not purchase_order:
        return None

    if status is not None:
        purchase_order.status = status
    if tracking_code is not None:
        purchase_order.tracking_code = tracking_code
    if arrival_date is not None:
        purchase_order.arrival_date = arrival_date
    if invoice_ref is not None:
        purchase_order.invoice_ref = invoice_ref

    db.commit()
    db.refresh(purchase_order)
    return purchase_order


def receive_purchase_order(
    db: Session,
    purchase_id: int,
    location: str = "Warehouse-1",
) -> PurchaseOrder | None:
    purchase_order = db.get(PurchaseOrder, purchase_id)
    if not purchase_order:
        return None

    items = list_purchase_items(db, purchase_id)

    for item in items:
        stock = (
            db.query(Stock)
            .filter(Stock.component_id == item.component_id)
            .first()
        )

        if stock:
            stock.actual_qty += item.qty
            stock.location = location
        else:
            db.add(
                Stock(
                    component_id=item.component_id,
                    actual_qty=item.qty,
                    location=location,
                )
            )

    purchase_order.status = "Received"
    db.commit()
    db.refresh(purchase_order)
    return purchase_order
