from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, and_, cast, or_
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, Field

from app.database import get_db
from app.models.auth import User
from app.models.inventory import Component, InventoryMovement, Stock
from app.models.production import ProductType, WorkflowTask
from app.schemas.inventory import ComponentCreate
from app.services.auth_service import require_roles
from app.services.inventory_movement_service import record_movement

router = APIRouter(tags=["Склад (Inventory)"])
INVENTORY_READ_ROLES = ("admin", "warehouse", "manager", "engineer", "production", "procurement")


class FinishedGoodsIssue(BaseModel):
    quantity: float = Field(gt=0)
    recipient: str = Field(min_length=1, max_length=255)
    note: Optional[str] = Field(None, max_length=1000)


def _component_payload(component: Component, quantity: float = 0.0):
    return {
        "id": component.id,
        "name": component.name,
        "part_number": component.part_number,
        "category": component.category or "Прочие компоненты",
        "package": component.package,
        "value": component.value,
        "value_numeric": component.value_numeric,
        "voltage": component.voltage,
        "specifications": component.specifications,
        "quantity": quantity or 0.0,
    }


def _apply_component_search(query, search: Optional[str]):
    if search and search.strip():
        search_words = search.strip().split()
        conditions = []
        for word in search_words:
            pattern = f"%{word}%"
            conditions.append(or_(
                Component.name.ilike(pattern),
                Component.category.ilike(pattern),
                Component.package.ilike(pattern),
                Component.value.ilike(pattern),
                Component.part_number.ilike(pattern),
                cast(Component.specifications, String).ilike(pattern),
            ))
        query = query.filter(and_(*conditions))
    return query


def _movement_payload(movement: InventoryMovement, users: dict[int, User], tasks: dict[int, WorkflowTask]):
    actor = users.get(movement.actor_user_id)
    counterparty = users.get(movement.counterparty_user_id)
    task = tasks.get(movement.task_id)
    return {
        "id": movement.id,
        "component_id": movement.component_id,
        "product_id": movement.product_id,
        "direction": movement.direction,
        "quantity": movement.quantity,
        "balance_after": movement.balance_after,
        "location": movement.location,
        "task_id": movement.task_id,
        "task_title": task.title if task else None,
        "order_id": movement.order_id,
        "actor_user_id": movement.actor_user_id,
        "actor_name": (actor.full_name or actor.username) if actor else None,
        "counterparty_user_id": movement.counterparty_user_id,
        "counterparty_name": (counterparty.full_name or counterparty.username) if counterparty else None,
        "counterparty_role": movement.counterparty_role,
        "recipient": movement.recipient,
        "note": movement.note,
        "created_at": movement.created_at,
    }


@router.get("/movements")
def get_inventory_movements(
    component_id: Optional[int] = Query(None),
    product_id: Optional[int] = Query(None),
    before_id: Optional[int] = Query(None, ge=1),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _=Depends(require_roles(*INVENTORY_READ_ROLES, "packer")),
):
    query = db.query(InventoryMovement)
    if component_id is not None:
        query = query.filter(InventoryMovement.component_id == component_id)
    if product_id is not None:
        query = query.filter(InventoryMovement.product_id == product_id)
    if before_id is not None:
        query = query.filter(InventoryMovement.id < before_id)
    movements = query.order_by(InventoryMovement.id.desc()).limit(limit + 1).all()
    page = movements[:limit]
    user_ids = {value for movement in page for value in (movement.actor_user_id, movement.counterparty_user_id) if value}
    task_ids = {movement.task_id for movement in page if movement.task_id}
    users = {user.id: user for user in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    tasks = {task.id: task for task in db.query(WorkflowTask).filter(WorkflowTask.id.in_(task_ids)).all()} if task_ids else {}
    return {
        "items": [_movement_payload(movement, users, tasks) for movement in page],
        "next_cursor": page[-1].id if len(movements) > limit and page else None,
        "has_more": len(movements) > limit,
    }


@router.get("/finished-goods")
def get_finished_goods(
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "warehouse", "manager", "production", "packer")),
):
    query = db.query(Stock, ProductType).join(ProductType, Stock.product_id == ProductType.id).filter(Stock.product_id.isnot(None))
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        query = query.filter(or_(ProductType.name.ilike(pattern), ProductType.sku.ilike(pattern), ProductType.drawing_number.ilike(pattern)))
    return [{
        "product_id": product.id,
        "name": product.name,
        "sku": product.sku,
        "drawing_number": product.drawing_number,
        "quantity": stock.actual_qty or 0,
        "location": stock.location,
    } for stock, product in query.order_by(ProductType.name).all()]


@router.post("/finished-goods/{product_id}/issue")
def issue_finished_goods(
    product_id: int,
    payload: FinishedGoodsIssue,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "warehouse")),
):
    stock = db.query(Stock).filter(Stock.product_id == product_id).with_for_update().first()
    if not stock:
        raise HTTPException(status_code=404, detail="Готовая продукция не найдена на складе")
    if (stock.actual_qty or 0) < payload.quantity:
        raise HTTPException(status_code=400, detail=f"Недостаточно готовой продукции. Доступно: {stock.actual_qty or 0}")
    stock.actual_qty -= payload.quantity
    record_movement(
        db,
        direction="outgoing",
        quantity=payload.quantity,
        balance_after=stock.actual_qty,
        product_id=product_id,
        location=stock.location,
        actor_user_id=user.id,
        recipient=payload.recipient,
        note=payload.note or "Выдача готовой продукции",
    )
    db.commit()
    return {"status": "success", "new_qty": stock.actual_qty}


@router.post("/components")
def create_component(
    data: ComponentCreate,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "warehouse")),
):
    """Регистрация нового компонента в справочнике."""
    existing = db.query(Component).filter(Component.part_number == data.part_number).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Компонент с артикулом {data.part_number} уже существует"
        )

    new_component = Component(
        name=data.name,
        part_number=data.part_number,
        category=data.category,
        package=data.package,
        value=data.value,
        value_numeric=data.value_numeric,
        voltage=data.voltage,
        specifications=data.specifications
    )

    db.add(new_component)
    db.commit()
    db.refresh(new_component)
    return new_component


@router.get("/components")
def get_components(
    search: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _=Depends(require_roles(*INVENTORY_READ_ROLES)),
):
    """Получение списка компонентов с фильтрацией по поисковому запросу."""
    query = _apply_component_search(db.query(Component), search)
    components = query.order_by(Component.category, Component.name).offset(offset).limit(limit).all()
    component_ids = [component.id for component in components]
    stock_map = {
        s.component_id: s.actual_qty
        for s in db.query(Stock).filter(Stock.component_id.in_(component_ids)).all()
    } if component_ids else {}

    return [_component_payload(comp, stock_map.get(comp.id, 0.0)) for comp in components]


@router.get("/components/page")
def get_components_page(
    search: Optional[str] = Query(None),
    categories: Optional[List[str]] = Query(None),
    after_id: Optional[int] = Query(None, ge=0),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(require_roles(*INVENTORY_READ_ROLES)),
):
    """Cursor-paginated inventory feed for large warehouses."""
    query = _apply_component_search(db.query(Component), search)
    if categories:
        query = query.filter(Component.category.in_(categories))
    if after_id is not None:
        query = query.filter(Component.id > after_id)

    components = query.order_by(Component.id).limit(limit + 1).all()
    has_more = len(components) > limit
    page = components[:limit]
    component_ids = [component.id for component in page]
    stock_map = {
        stock.component_id: stock.actual_qty
        for stock in db.query(Stock).filter(Stock.component_id.in_(component_ids)).all()
    } if component_ids else {}

    return {
        "items": [_component_payload(component, stock_map.get(component.id, 0.0)) for component in page],
        "next_cursor": page[-1].id if has_more and page else None,
        "has_more": has_more,
    }


@router.get("/components/search")
def search_components(
    q: str = Query("", description="Поиск по названию, артикулу, категории, корпусу или номиналу"),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    _=Depends(require_roles(*INVENTORY_READ_ROLES)),
):
    """Короткий поиск компонентов для dropdown-подбора BOM без загрузки всего склада."""
    query = _apply_component_search(db.query(Component), q)
    components = query.order_by(Component.part_number, Component.name).limit(limit).all()
    component_ids = [component.id for component in components]
    stock_map = {
        s.component_id: s.actual_qty
        for s in db.query(Stock).filter(Stock.component_id.in_(component_ids)).all()
    } if component_ids else {}

    return [_component_payload(comp, stock_map.get(comp.id, 0.0)) for comp in components]


@router.get("/components/categories", response_model=List[str])
def get_unique_categories(
    db: Session = Depends(get_db),
    _=Depends(require_roles(*INVENTORY_READ_ROLES)),
):
    """Получение списка всех уникальных категорий."""
    categories = db.query(Component.category).distinct().all()
    result = [cat[0] for cat in categories if cat[0]]

    return sorted(result) if result else ["Резисторы", "Конденсаторы", "Микросхемы", "Прочее"]


@router.get("/components/{component_id}")
def get_component_by_id(
    component_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles(*INVENTORY_READ_ROLES)),
):
    """Получение подробной карточки компонента по ID."""
    component = db.query(Component).get(component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Компонент не найден")
    return component


@router.post("/incoming")
def add_stock(
    component_id: int,
    quantity: float,
    location: str = "Warehouse-1",
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "warehouse")),
):
    """Оприходование количества компонента на склад."""
    if not db.query(Component).get(component_id):
        raise HTTPException(status_code=404, detail="Компонент не найден")

    stock_item = db.query(Stock).filter(Stock.component_id == component_id).first()

    if stock_item:
        stock_item.actual_qty += quantity
        stock_item.location = location
    else:
        stock_item = Stock(component_id=component_id, actual_qty=quantity, location=location)
        db.add(stock_item)

    db.commit()
    return {"status": "success", "new_qty": stock_item.actual_qty}


@router.put("/components/{component_id}")
def update_component(
    component_id: int,
    data: ComponentCreate,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "warehouse")),
):
    """Обновление данных существующего компонента."""
    component = db.query(Component).get(component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Компонент не найден")

    for key, value in data.dict().items():
        setattr(component, key, value)

    db.commit()
    db.refresh(component)
    return {"status": "success", "component": component}


@router.delete("/components/{component_id}")
def delete_component(
    component_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "warehouse")),
):
    """Удаление компонента и связанных с ним остатков."""
    component = db.query(Component).get(component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Компонент не найден")

    db.query(Stock).filter(Stock.component_id == component_id).delete()
    db.delete(component)
    db.commit()

    return {"status": "success", "message": "Компонент удален"}


@router.patch("/components/{component_id}/quantity")
def update_stock_quantity(
    component_id: int,
    new_quantity: float,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "warehouse")),
):
    """Прямое обновление количества компонента на складе."""
    stock = db.query(Stock).filter(Stock.component_id == component_id).first()
    if not stock:
        stock = Stock(component_id=component_id, actual_qty=new_quantity)
        db.add(stock)
    else:
        stock.actual_qty = new_quantity

    db.commit()
    return {"status": "success", "new_qty": stock.actual_qty}
