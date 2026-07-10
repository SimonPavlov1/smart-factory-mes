from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.procurement import PurchaseOrder, PurchaseItem
from app.models.inventory import Stock
from app.services.auth_service import require_roles

router = APIRouter(tags=["Закупки (Procurement)"])

@router.post("/orders")
def create_purchase_order(
    supplier: str,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "manager")),
):
    """Создать новый черновик заказа поставщику."""
    new_order = PurchaseOrder(supplier_name=supplier, status="Draft")
    db.add(new_order)
    db.commit()
    db.refresh(new_order)
    return new_order

@router.post("/orders/{order_id}/receive")
def receive_order(
    order_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "warehouse")),
):
    """
    Финальная приемка заказа.
    Переводит статус в 'Received' и автоматически пополняет остатки на складе.
    """
    order = db.query(PurchaseOrder).get(order_id)
    if not order or order.status == "Received":
        raise HTTPException(status_code=400, detail="Заказ не найден или уже принят")

    for item in order.items:
        stock = db.query(Stock).filter(Stock.component_id == item.component_id).first()
        if stock:
            stock.actual_qty += item.qty
        else:
            new_stock = Stock(component_id=item.component_id, actual_qty=item.qty)
            db.add(new_stock)

    order.status = "Received"
    db.commit()
    return {"message": "Заказ принят, склад обновлен"}
