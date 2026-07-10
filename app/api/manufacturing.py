from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models.production import Order, OrderItem, ProductBOM, ProductType, Reservation
from app.models.inventory import Stock, Component  # Component используется для вытягивания наименований деталей
from app.services.reservation_service import reserve_components
from app.services.production_planning import get_bom_requirements
from app.services.auth_service import require_roles

# ИМПОРТ СХЕМ: Подтягиваем переписанные схемы из файла
from app.schemas.order import OrderCreate, OrderOut

router = APIRouter(prefix="/manufacturing", tags=["Производство (Заказы)"])


def _add_material(total_needed_items: dict, component_id: int, qty: float):
    if not component_id:
        raise HTTPException(status_code=400, detail="В BOM есть непривязанная позиция без складского компонента")
    if qty <= 0:
        raise HTTPException(status_code=400, detail=f"Некорректное количество компонента ID {component_id}: {qty}")

    if component_id in total_needed_items:
        total_needed_items[component_id]["qty"] += qty
    else:
        total_needed_items[component_id] = {"component_id": component_id, "qty": qty}


def _calculate_materials_for_items(order_items, db: Session):
    total_needed_items = {}

    for item in order_items:
        if item.quantity <= 0:
            raise HTTPException(status_code=400, detail=f"Некорректное количество изделия ID {item.product_id}")

        needed = get_bom_requirements(item.product_id, item.quantity, db)
        for material in needed:
            _add_material(total_needed_items, material["component_id"], material["qty"])

    return total_needed_items


def _materials_list(total_needed_items: dict):
    return list(total_needed_items.values())


def _product_label(product: ProductType):
    if not product:
        return "Неизвестное изделие"
    return product.name if not product.sku else f"{product.name} ({product.sku})"


def _append_bom_summary_item(result_map: dict, item_data: dict):
    key = (
        item_data["device"],
        item_data["assembly"],
        item_data["category"],
        item_data["name"],
        item_data["sku"],
    )

    if key not in result_map:
        result_map[key] = item_data
        return

    result_map[key]["qty"] += item_data["qty"]
    if item_data.get("designators"):
        existing = result_map[key].get("designators")
        result_map[key]["designators"] = (
            f"{existing}, {item_data['designators']}" if existing else item_data["designators"]
        )


def _collect_structured_bom(product_id: int, multiplier: float, device: str, assembly: str, result_map: dict,
                            db: Session, visited=None):
    visited = visited or set()
    if product_id in visited:
        raise HTTPException(status_code=400, detail=f"Обнаружен циклический BOM у изделия ID {product_id}")

    current_visited = visited | {product_id}
    bom_items = db.query(ProductBOM).filter(ProductBOM.product_id == product_id).all()

    for bom in bom_items:
        total_qty = bom.quantity * multiplier

        if bom.resource_type == "component":
            component = db.query(Component).filter(Component.id == bom.resource_id).first() if bom.resource_id else None
            _append_bom_summary_item(result_map, {
                "id": component.id if component else f"bom-{bom.id}",
                "component_id": component.id if component else None,
                "bom_item_id": bom.id,
                "name": component.name if component else bom.design_name,
                "sku": component.part_number if component else "—",
                "qty": total_qty,
                "device": device,
                "assembly": assembly,
                "category": component.category if component and component.category else "Покупные компоненты",
                "designators": bom.designators,
                "item_type": "purchased_component" if component else "unresolved_purchase",
            })
            continue

        if bom.resource_type in ["product", "subassembly"]:
            sub_product = db.query(ProductType).filter(ProductType.id == bom.resource_id).first() if bom.resource_id else None
            subassembly_name = _product_label(sub_product) if sub_product else bom.design_name
            assembly_path = (
                subassembly_name if assembly == "Основной состав" else f"{assembly} / {subassembly_name}"
            )

            if sub_product and db.query(ProductBOM).filter(ProductBOM.product_id == sub_product.id).first():
                _collect_structured_bom(
                    product_id=sub_product.id,
                    multiplier=total_qty,
                    device=device,
                    assembly=assembly_path,
                    result_map=result_map,
                    db=db,
                    visited=current_visited,
                )
                continue

            _append_bom_summary_item(result_map, {
                "id": sub_product.id if sub_product else f"bom-{bom.id}",
                "component_id": None,
                "bom_item_id": bom.id,
                "name": subassembly_name,
                "sku": sub_product.sku if sub_product and sub_product.sku else "—",
                "qty": total_qty,
                "device": device,
                "assembly": assembly,
                "category": "Покупные изделия и узлы",
                "designators": bom.designators,
                "item_type": "purchased_product",
            })
            continue

        _append_bom_summary_item(result_map, {
            "id": f"bom-{bom.id}",
            "component_id": None,
            "bom_item_id": bom.id,
            "name": bom.design_name,
            "sku": "—",
            "qty": total_qty,
            "device": device,
            "assembly": assembly,
            "category": "Непривязанные позиции",
            "designators": bom.designators,
            "item_type": "unresolved_purchase",
        })


def _structured_bom_summary_for_order(order_items, db: Session):
    result_map = {}

    for item in order_items:
        if item.quantity <= 0:
            raise HTTPException(status_code=400, detail=f"Некорректное количество изделия ID {item.product_id}")

        product = db.query(ProductType).filter(ProductType.id == item.product_id).first()
        if not product:
            raise HTTPException(status_code=404, detail=f"Изделие ID {item.product_id} не найдено")

        device = _product_label(product)
        _collect_structured_bom(
            product_id=product.id,
            multiplier=item.quantity,
            device=device,
            assembly="Основной состав",
            result_map=result_map,
            db=db,
        )

    return sorted(
        result_map.values(),
        key=lambda row: (row["device"], row["assembly"], row["category"], row["name"], row["sku"]),
    )


@router.get("/orders", response_model=List[OrderOut], summary="Получить список всех заказов")
def get_production_orders(
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "manager", "warehouse", "production")),
):
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
def create_production_order(
    payload: OrderCreate,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "manager", "production")),
):
    """
    Создает заказ для конкретного заказчика с несколькими изделиями.
    Суммирует требования BOM и резервирует компоненты.
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="Заказ должен содержать хотя бы одно изделие")

    try:
        for item in payload.items:
            if item.quantity <= 0:
                raise HTTPException(status_code=400, detail="Количество изделия должно быть больше нуля")

        new_order = Order(
            customer_name=payload.customer_name,
            status="Reserved"
        )
        db.add(new_order)
        db.flush()  # Получаем id созданного заказа

        order_items = []
        for item in payload.items:
            order_item = OrderItem(
                order_id=new_order.id,
                product_id=item.product_id,
                quantity=item.quantity
            )
            db.add(order_item)
            order_items.append(order_item)

        total_needed_items = _calculate_materials_for_items(order_items, db)
        materials_list = _materials_list(total_needed_items)

        reserve_components(db, materials_list)

        for material in materials_list:
            db.add(Reservation(
                order_id=new_order.id,
                component_id=material["component_id"],
                qty=material["qty"]
            ))

        db.commit()
        return {"status": "success", "order_id": new_order.id, "details": materials_list}

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/orders/{order_id}/issue-materials", summary="Выдача материалов под весь заказ")
def issue_materials_for_order(
    order_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "warehouse")),
):
    """
    Списывает зарезервированные материалы под все позиции этого заказа.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if order.status not in ["In Progress", "Reserved"]:
        raise HTTPException(status_code=400, detail="Материалы уже выданы или заказ завершен")

    reservations = db.query(Reservation).filter(Reservation.order_id == order_id).all()
    if not reservations:
        if order.status != "In Progress":
            raise HTTPException(status_code=400, detail="По заказу нет сохраненного резерва материалов")

        order_items = db.query(OrderItem).filter(OrderItem.order_id == order_id).all()
        total_needed_items = _calculate_materials_for_items(order_items, db)
        reservations = [
            Reservation(order_id=order.id, component_id=material["component_id"], qty=material["qty"])
            for material in _materials_list(total_needed_items)
        ]
        for reservation in reservations:
            db.add(reservation)

    try:
        for item in reservations:
            stock = db.query(Stock).filter(Stock.component_id == item.component_id).with_for_update().first()
            if not stock:
                raise HTTPException(status_code=404, detail=f"Компонент {item.component_id} отсутствует на складе")

            stock.actual_qty = stock.actual_qty or 0
            stock.reserved_qty = stock.reserved_qty or 0

            if stock.actual_qty < item.qty:
                raise HTTPException(
                    status_code=400,
                    detail=f"Недостаточно товара на складе для компонента ID {item.component_id}"
                )
            if stock.reserved_qty < item.qty:
                raise HTTPException(
                    status_code=400,
                    detail=f"Недостаточно зарезервировано для компонента ID {item.component_id}"
                )

            stock.actual_qty -= item.qty
            stock.reserved_qty -= item.qty

        order.status = "Materials Issued"
        db.commit()
    except HTTPException:
        db.rollback()
        raise

    return {"status": "success", "message": "Материалы по всем позициям выданы в цех"}


# =====================================================================
# ИСПРАВЛЕННЫЙ ЭНДПОИНТ: Сводная ведомость комплектующих с защитой от AttributeError
# =====================================================================
@router.get("/orders/{order_id}/bom-summary", summary="Сводная комплектация для PDF")
def get_order_bom_summary(
    order_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "manager", "warehouse", "production")),
):
    """
    Возвращает комплектацию заказа с сохранением структуры:
    изделие верхнего уровня -> сборочная единица -> покупные компоненты/изделия.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    order_items = db.query(OrderItem).filter(OrderItem.order_id == order_id).all()
    return _structured_bom_summary_for_order(order_items, db)
