from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, and_, cast, or_
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db
from app.models.inventory import Component, Stock
from app.schemas.inventory import ComponentCreate
from app.services.auth_service import require_roles

router = APIRouter(tags=["Склад (Inventory)"])
INVENTORY_READ_ROLES = ("admin", "warehouse", "manager", "engineer", "production", "procurement")


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
