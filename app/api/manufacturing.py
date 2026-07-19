from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models.auth import User
from app.models.production import Item, Order, OrderItem, ProductBOM, ProductType, Reservation, WorkflowTask
from app.models.inventory import Stock, Component  # Component используется для вытягивания наименований деталей
from app.services.reservation_service import reserve_components
from app.services.production_planning import get_bom_requirements
from app.services.auth_service import require_roles, user_roles
from app.services.workflow_service import create_initial_order_tasks, find_shortages

# ИМПОРТ СХЕМ: Подтягиваем переписанные схемы из файла
from app.schemas.order import OrderCreate, OrderOut

router = APIRouter(prefix="/manufacturing", tags=["Производство (Заказы)"])

ORDER_STAGES = [
    {
        "key": "procurement",
        "title": "Закупка",
        "description": "Оформление недостающих комплектующих",
        "task_types": ["procurement_purchase"],
    },
    {
        "key": "accounting",
        "title": "Оплата",
        "description": "Оплата счетов по закупке",
        "task_types": ["accounting_payment"],
    },
    {
        "key": "warehouse_receive",
        "title": "Приемка на склад",
        "description": "Приход комплектующих от поставщика",
        "task_types": ["warehouse_receive_components"],
    },
    {
        "key": "warehouse_issue",
        "title": "Выдача комплектующих",
        "description": "Передача комплекта сборщику",
        "task_types": ["warehouse_issue_materials"],
    },
    {
        "key": "assembler_receive",
        "title": "Получение сборщиком",
        "description": "Подтверждение получения комплекта",
        "task_types": ["assembler_receive_materials"],
    },
    {
        "key": "assembly",
        "title": "Сборка",
        "description": "Сборка изделий по заказу",
        "task_types": ["assembler_build"],
    },
    {
        "key": "testing",
        "title": "Тестирование",
        "description": "Проверка и фиксация брака",
        "task_types": ["tester_check"],
    },
    {
        "key": "repair",
        "title": "Ремонт брака",
        "description": "Устранение выявленных дефектов",
        "task_types": ["repair_defects"],
    },
    {
        "key": "packing",
        "title": "Упаковка",
        "description": "Передача годных изделий на упаковку",
        "task_types": ["packer_pack"],
    },
    {
        "key": "finished_goods",
        "title": "Склад готовой продукции",
        "description": "Оприходование готовых изделий",
        "task_types": ["warehouse_finished_goods"],
    },
]


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
    children_by_parent = {}
    for item in bom_items:
        if item.parent_id:
            children_by_parent.setdefault(item.parent_id, []).append(item)

    def visit_bom(bom, current_multiplier, current_assembly):
        item_type = bom.item_type or ("assembly" if bom.resource_type in ["product", "subassembly"] else "component")
        total_qty = bom.quantity * current_multiplier

        if item_type == "operation":
            _append_bom_summary_item(result_map, {
                "id": f"operation-{bom.id}",
                "component_id": None,
                "bom_item_id": bom.id,
                "name": bom.design_name,
                "sku": bom.operation_role or "—",
                "qty": total_qty,
                "device": device,
                "assembly": current_assembly,
                "category": "Работы и операции",
                "designators": bom.designators,
                "item_type": "operation",
            })
            return

        if item_type == "component" and bom.resource_type == "component":
            component = db.query(Component).filter(Component.id == bom.resource_id).first() if bom.resource_id else None
            _append_bom_summary_item(result_map, {
                "id": component.id if component else f"bom-{bom.id}",
                "component_id": component.id if component else None,
                "bom_item_id": bom.id,
                "name": component.name if component else bom.design_name,
                "sku": component.part_number if component else "—",
                "qty": total_qty,
                "device": device,
                "assembly": current_assembly,
                "category": component.category if component and component.category else "Покупные компоненты",
                "designators": bom.designators,
                "item_type": "purchased_component" if component else "unresolved_purchase",
            })
            return

        if item_type == "assembly":
            sub_product = db.query(ProductType).filter(ProductType.id == bom.resource_id).first() if bom.resource_id else None
            subassembly_name = _product_label(sub_product) if sub_product else bom.design_name
            assembly_path = (
                subassembly_name if current_assembly == "Основной состав" else f"{current_assembly} / {subassembly_name}"
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
                return

            local_children = children_by_parent.get(bom.id, [])
            if local_children:
                for child in local_children:
                    visit_bom(child, total_qty, assembly_path)
                return

            _append_bom_summary_item(result_map, {
                "id": sub_product.id if sub_product else f"bom-{bom.id}",
                "component_id": None,
                "bom_item_id": bom.id,
                "name": subassembly_name,
                "sku": sub_product.sku if sub_product and sub_product.sku else "—",
                "qty": total_qty,
                "device": device,
                "assembly": current_assembly,
                "category": "Покупные изделия и узлы",
                "designators": bom.designators,
                "item_type": "purchased_product",
            })
            return

        _append_bom_summary_item(result_map, {
            "id": f"bom-{bom.id}",
            "component_id": None,
            "bom_item_id": bom.id,
            "name": bom.design_name,
            "sku": "—",
            "qty": total_qty,
            "device": device,
            "assembly": current_assembly,
            "category": "Непривязанные позиции",
            "designators": bom.designators,
            "item_type": "unresolved_purchase",
        })

    for bom in bom_items:
        if bom.parent_id:
            continue
        visit_bom(bom, multiplier, assembly)


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


def _parse_optional_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректная дата поставки")


def _user_payload(user: User | None):
    if not user:
        return None
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "roles": user_roles(user),
    }


def _task_payload(task: WorkflowTask, users_by_id: dict[int, User] | None = None):
    assigned_user = users_by_id.get(task.assigned_user_id) if users_by_id and task.assigned_user_id else None
    return {
        "id": task.id,
        "type": task.type,
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "status": task.status,
        "assigned_user_id": task.assigned_user_id,
        "assigned_user": _user_payload(assigned_user),
        "payload": task.payload or {},
        "created_at": task.created_at,
        "due_date": task.due_date,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
    }


def _stage_status(tasks: list[WorkflowTask]) -> str:
    if not tasks:
        return "not_created"
    if all(task.status == "done" for task in tasks):
        return "done"
    if any(task.status == "in_progress" for task in tasks):
        return "in_progress"
    if any(task.status in ["assigned", "open", "waiting_delivery"] for task in tasks):
        return "assigned"
    return tasks[-1].status or "assigned"


def _order_payload(order: Order, db: Session):
    tasks = (
        db.query(WorkflowTask)
        .filter(WorkflowTask.order_id == order.id)
        .order_by(WorkflowTask.created_at.asc(), WorkflowTask.id.asc())
        .all()
    )
    tasks_by_type = {}
    for task in tasks:
        tasks_by_type.setdefault(task.type, []).append(task)
    user_ids = [task.assigned_user_id for task in tasks if task.assigned_user_id]
    users_by_id = {
        user.id: user
        for user in db.query(User).filter(User.id.in_(user_ids)).all()
    } if user_ids else {}

    stages = []
    for stage in ORDER_STAGES:
        stage_tasks = []
        for task_type in stage["task_types"]:
            stage_tasks.extend(tasks_by_type.get(task_type, []))
        stage_tasks.sort(key=lambda task: (task.created_at, task.id))
        stages.append({
            "key": stage["key"],
            "title": stage["title"],
            "description": stage["description"],
            "status": _stage_status(stage_tasks),
            "tasks": [_task_payload(task, users_by_id) for task in stage_tasks],
        })

    return {
        "id": order.id,
        "customer_name": order.customer_name,
        "status": order.status,
        "created_at": order.created_at,
        "planned_delivery_date": order.planned_delivery_date,
        "items": [
            {
                "id": item.id,
                "product_id": item.product_id,
                "quantity": item.quantity,
                "product": {
                    "id": item.product.id,
                    "name": item.product.name,
                    "sku": item.product.sku,
                    "drawing_number": item.product.drawing_number,
                } if item.product else None,
            }
            for item in order.items
        ],
        "tasks": [_task_payload(task, users_by_id) for task in tasks],
        "stages": stages,
    }


@router.get("/orders", response_model=List[OrderOut], summary="Получить список всех заказов")
def get_production_orders(
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "manager", "warehouse", "production", "procurement", "assembler", "tester", "repair_engineer", "packer")),
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
            status="Created",
            planned_delivery_date=_parse_optional_date(payload.planned_delivery_date),
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
        shortages = find_shortages(db, materials_list)

        if not shortages:
            reserve_components(db, materials_list)

            for material in materials_list:
                db.add(Reservation(
                    order_id=new_order.id,
                    component_id=material["component_id"],
                    qty=material["qty"]
                ))

        create_initial_order_tasks(db, new_order, materials_list, shortages)

        db.commit()
        return {
            "status": "success",
            "order_id": new_order.id,
            "order_status": new_order.status,
            "details": materials_list,
            "shortages": shortages,
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders/{order_id}", summary="Детальная карточка заказа с производственной цепочкой")
def get_production_order_detail(
    order_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "manager", "warehouse", "production", "procurement", "assembler", "tester", "repair_engineer", "packer", "accounting")),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return _order_payload(order, db)


@router.delete("/orders/{order_id}", summary="Удалить производственный заказ")
def delete_production_order(
    order_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "manager", "production")),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    reservations = db.query(Reservation).filter(Reservation.order_id == order_id).all()
    if order.status in ["Reserved", "In Progress", "Procurement Required"]:
        for reservation in reservations:
            stock = db.query(Stock).filter(Stock.component_id == reservation.component_id).first()
            if stock:
                stock.reserved_qty = max((stock.reserved_qty or 0) - reservation.qty, 0)

    db.query(WorkflowTask).filter(WorkflowTask.order_id == order_id).delete(synchronize_session=False)
    db.query(Item).filter(Item.order_id == order_id).delete(synchronize_session=False)
    db.query(Reservation).filter(Reservation.order_id == order_id).delete(synchronize_session=False)
    db.query(OrderItem).filter(OrderItem.order_id == order_id).delete(synchronize_session=False)
    db.delete(order)
    db.commit()
    return {"status": "success", "detail": "Заказ удален"}


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
    _=Depends(require_roles("admin", "manager", "warehouse", "production", "procurement", "assembler", "tester", "repair_engineer", "packer")),
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
