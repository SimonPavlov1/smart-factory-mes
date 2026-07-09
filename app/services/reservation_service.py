from sqlalchemy.orm import Session
from fastapi import HTTPException
from app.models.inventory import Stock


def reserve_components(db: Session, items: list):
    """
    items: список словарей [{"component_id": int, "qty": float}, ...]
    """
    for item in items:
        # Используем with_for_update(), чтобы БД заблокировала строку
        # и никто другой не мог изменить остатки в этот момент
        stock = db.query(Stock).filter(Stock.component_id == item["component_id"]).with_for_update().first()

        if not stock:
            raise HTTPException(status_code=404, detail=f"Компонент {item['component_id']} отсутствует на складе")

        stock.actual_qty = stock.actual_qty or 0
        stock.reserved_qty = stock.reserved_qty or 0

        # Считаем доступный остаток (то, что есть минус то, что уже обещано)
        available = stock.actual_qty - stock.reserved_qty

        if available < item["qty"]:
            raise HTTPException(status_code=400,
                                detail=f"Недостаточно деталей {item['component_id']}. Свободно: {available}")

        # Резервируем
        stock.reserved_qty += item["qty"]
        # Сохраняем изменения (flush)
        db.flush()
