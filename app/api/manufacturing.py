from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models.production import Order, OrderItem
from app.models.inventory import Stock, Component  # Component используется для вытягивания наименований деталей
from app.services.reservation_service import reserve_components
from app.services.production_planning import get_bom_requirements

# ИМПОРТ СХЕМ: Подтягиваем переписанные схемы из файла
from app.schemas.order import OrderCreate, OrderOut

router = APIRouter(prefix="/manufacturing", tags=["Производство (Заказы)"])


@router.get("/orders", response_model=List[OrderOut], summary="Получить список всех заказов")
def get_production_orders(db: Session = Depends(get_db)):
    """
    Возвращает список всех заказов.
    Благодаря response_model=List[OrderOut], Pydantic автоматически трансформирует
    каждый объект, добавив внутрь позиций реальные name и sku изделий.
    """
    try:
        orders = db.query(Order).all()
        return orders
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка БД: {str(e)}")


@router.post("/orders", summary="Создать многопозиционный заказ")
def create_production_order(payload: OrderCreate, db: Session = Depends(get_db)):
    """
    Создает заказ для конкретного заказчика с несколькими изделиями.
    Суммирует требования BOM и резервирует компоненты.
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="Заказ должен содержать хотя бы одно изделие")

    # 1. Собираем все требования по материалам для всех позиций вместе
    total_needed_items = {}

    for item in payload.items:
        # Получаем требования для конкретного изделия
        needed_for_item = get_bom_requirements(item.product_id, item.quantity, db)

        # Суммируем в общий словарь, чтобы не резервировать по отдельности
        for material in needed_for_item:
            c_id = material["component_id"]
            if c_id in total_needed_items:
                total_needed_items[c_id]["qty"] += material["qty"]
            else:
                total_needed_items[c_id] = {"component_id": c_id, "qty": material["qty"]}

    # Переводим обратно в список для сервиса резервирования
    materials_list = list(total_needed_items.values())

    try:
        # 2. Резервируем суммарные компоненты на складе
        reserve_components(db, materials_list)

        # 3. Создаем главный заказ
        new_order = Order(
            customer_name=payload.customer_name,
            status="In Progress"
        )
        db.add(new_order)
        db.flush()  # Получаем id созданного заказа

        # 4. Сохраняем позиции заказа в связующую таблицу OrderItem
        for item in payload.items:
            order_item = OrderItem(
                order_id=new_order.id,
                product_id=item.product_id,
                quantity=item.quantity
            )
            db.add(order_item)

        db.commit()
        return {"status": "success", "order_id": new_order.id, "details": materials_list}

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/orders/{order_id}/issue-materials", summary="Выдача материалов под весь заказ")
def issue_materials_for_order(order_id: int, db: Session = Depends(get_db)):
    """
    Списывает зарезервированные материалы под все позиции этого заказа.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if order.status != "In Progress":
        raise HTTPException(status_code=400, detail="Материалы уже выданы или заказ завершен")

    # Собираем все позиции заказа
    order_items = db.query(OrderItem).filter(OrderItem.order_id == order_id).all()

    total_needed_items = {}
    for item in order_items:
        needed = get_bom_requirements(item.product_id, item.quantity, db)
        for material in needed:
            c_id = material["component_id"]
            if c_id in total_needed_items:
                total_needed_items[c_id]["qty"] += material["qty"]
            else:
                total_needed_items[c_id] = {"component_id": c_id, "qty": material["qty"]}

    # Списание со склада
    for item in total_needed_items.values():
        stock = db.query(Stock).filter(Stock.component_id == item["component_id"]).with_for_update().first()
        if stock:
            # Защита от ухода склада в минус
            if stock.actual_qty < item["qty"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Недостаточно товара на складе для компонента ID {item['component_id']}"
                )
            stock.actual_qty -= item["qty"]
            stock.reserved_qty -= item["qty"]

    order.status = "Materials Issued"
    db.commit()

    return {"status": "success", "message": "Материалы по всем позициям выданы в цех"}


# =====================================================================
# ИСПРАВЛЕННЫЙ ЭНДПОИНТ: Сводная ведомость комплектующих с защитой от AttributeError
# =====================================================================
@router.get("/orders/{order_id}/bom-summary", summary="Сводная комплектация для PDF")
def get_order_bom_summary(order_id: int, db: Session = Depends(get_db)):
    """
    Агрегирует спецификации (BOM) со всех позиций текущего заказа,
    безопасно подтягивает наименования и артикулы компонентов со склада.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    # Ищем все позиции (изделия) внутри этого заказа
    order_items = db.query(OrderItem).filter(OrderItem.order_id == order_id).all()

    total_needed_items = {}

    # Считаем суммарную потребность по материалам
    for item in order_items:
        needed = get_bom_requirements(item.product_id, item.quantity, db)

        # Резервный сценарий: если сервис вернул пустоту, пробуем вытащить данные через связи моделей
        if not needed and hasattr(item, "product") and hasattr(item.product, "bom_items"):
            for bom in item.product.bom_items:
                c_id = bom.component_id
                required_qty = bom.quantity * item.quantity
                if c_id in total_needed_items:
                    total_needed_items[c_id]["qty"] += required_qty
                else:
                    total_needed_items[c_id] = {"qty": required_qty}
            continue

        # Основной сценарий сборки данных
        if needed:
            for material in needed:
                c_id = material["component_id"]
                if c_id in total_needed_items:
                    total_needed_items[c_id]["qty"] += material["qty"]
                else:
                    total_needed_items[c_id] = {"qty": material["qty"]}

    # Обогащаем данные информацией из таблицы Component с защитой от отсутствия полей
    result = []
    for c_id, info in total_needed_items.items():
        component = db.query(Component).filter(Component.id == c_id).first()

        # Динамически ищем поле артикула, чтобы избежать AttributeError
        component_sku = "—"
        if component:
            if hasattr(component, "sku") and component.sku:
                component_sku = component.sku
            elif hasattr(component, "part_number") and component.part_number:
                component_sku = component.part_number
            elif hasattr(component, "code") and component.code:
                component_sku = component.code

        result.append({
            "id": c_id,
            "name": component.name if component else f"Компонент ID {c_id}",
            "sku": component_sku,
            "qty": info["qty"]
        })

    return result