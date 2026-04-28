from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.inventory import Component, Stock

router = APIRouter(prefix="/inventory", tags=["Склад (Inventory)"])


@router.get("/components")
def get_components(db: Session = Depends(get_db)):
    """
    Получить полный справочник ТМЦ.
    Используется для выбора деталей при ручном маппинге.
    """
    return db.query(Component).all()


@router.get("/components/{component_id}")
def get_component_by_id(component_id: int, db: Session = Depends(get_db)):
    """Получить детальную информацию о конкретном компоненте по его ID."""
    component = db.query(Component).get(component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Компонент не найден")
    return component


@router.post("/incoming")
def add_stock(component_id: int, quantity: float, location: str = "Warehouse-1", db: Session = Depends(get_db)):
    """
    Оприходование ТМЦ на склад.
    Увеличивает физический остаток (actual_qty).
    """
    stock_item = db.query(Stock).filter(Stock.component_id == component_id).first()

    if stock_item:
        stock_item.actual_qty += quantity
        stock_item.location = location
    else:
        stock_item = Stock(component_id=component_id, actual_qty=quantity, location=location)
        db.add(stock_item)

    db.commit()
    return {"status": "success", "new_qty": stock_item.actual_qty}