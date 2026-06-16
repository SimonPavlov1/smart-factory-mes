from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.production import Order
from app.models.inventory import Stock
from app.services.reservation_service import reserve_components
from app.services.production_planning import get_bom_requirements

# Если в main.py вы подключаете роутер БЕЗ prefix="/api",
# то итоговые пути будут: /manufacturing/orders
router = APIRouter(prefix="/manufacturing", tags=["Производство (Заказы)"])


@router.get("/orders", summary="Получить список всех производственных заказов")
def get_production_orders(db: Session = Depends(get_db)):
    """
    Возвращает список всех существующих заказов для фронтенда.
    """
    try:
        orders = db.query(Order).all()
        return orders
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка БД: {str(e)}")


@router.post("/orders", summary="Создать производственный заказ")
def create_production_order(product_id: int, quantity: int, db: Session = Depends(get_db)):
    """
    1. Рассчитывает потребности BOM.
    2. Резервирует детали на складе.
    3. Создает заказ со статусом 'In Progress'.
    """
    needed_items = get_bom_requirements(product_id, quantity, db)

    try:
        # Резервирование (авто-проверка наличия)
        reserve_components(db, needed_items)

        # Создание заказа
        new_order = Order(product_id=product_id, target_qty=quantity, status="In Progress")
        db.add(new_order)
        db.commit()

        return {"status": "success", "order_id": new_order.id, "details": needed_items}

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/orders/{order_id}/issue-materials", summary="Выдача материалов в производство")
def issue_materials_for_order(order_id: int, db: Session = Depends(get_db)):
    """
    Физическое списание материалов со склада для заказа.
    Переводит заказ в статус 'In Production'.
    """
    order = db.query(Order).filter(Order.id == order_id).first()

    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    if order.status != "In Progress":
        raise HTTPException(status_code=400, detail=f"Заказ нельзя выдать, текущий статус: {order.status}")

    # Расчет того, что нужно списать
    needed_items = get_bom_requirements(order.product_id, order.target_qty, db)

    # Списание
    for item in needed_items:
        stock = db.query(Stock).filter(Stock.component_id == item["component_id"]).with_for_update().first()

        if stock:
            # Уменьшаем физические остатки и резерв
            stock.actual_qty -= item["qty"]
            stock.reserved_qty -= item["qty"]

    # Обновление статуса
    order.status = "In Production"
    db.commit()

    return {"status": "success", "message": "Материалы выданы, заказ в производстве"}