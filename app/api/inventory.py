from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from app.database import get_db
from app.models.inventory import Component, Stock
from app.schemas.inventory import ComponentCreate

router = APIRouter(tags=["Склад (Inventory)"])


@router.post("/components", response_model=None)
def create_component(data: ComponentCreate, db: Session = Depends(get_db)):
    """
    Регистрация новой позиции в справочнике ТМЦ.

    Этот метод создает "паспорт" детали. Здесь мы используем поле specifications (JSON),
    чтобы сохранить уникальные свойства (например, для микросхем — интерфейсы,
    для резисторов — точность).
    """
    # Проверка на дубликаты по MPN (артикулу производителя)
    existing = db.query(Component).filter(Component.part_number == data.part_number).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Компонент с артикулом {data.part_number} уже существует в базе"
        )

    # Создание записи с поддержкой гибких полей
    new_component = Component(
        name=data.name,
        part_number=data.part_number,
        category=data.category,
        package=data.package,
        value=data.value,
        voltage=data.voltage,
        specifications=data.specifications  # SQLAlchemy сама упакует dict в JSON
    )

    db.add(new_component)
    db.commit()
    db.refresh(new_component)
    return new_component


@router.get("/components")
def get_components(db: Session = Depends(get_db)):
    """
    Получить весь список зарегистрированных компонентов.
    Нужен для проверки, какие ID присвоены вашим деталям.
    """
    return db.query(Component).all()


@router.get("/components/{component_id}")
def get_component_by_id(component_id: int, db: Session = Depends(get_db)):
    """Получить подробную карточку компонента, включая его JSON-характеристики."""
    component = db.query(Component).get(component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Компонент не найден")
    return component


@router.post("/incoming")
def add_stock(component_id: int, quantity: float, location: str = "Warehouse-1", db: Session = Depends(get_db)):
    """
    Оприходование (приемка) ТМЦ на физический склад.

    Сначала проверяет наличие 'паспорта' в справочнике. Если карточка есть,
    создает или обновляет запись в таблице Stock (остатки).
    """
    # Защита: нельзя положить на склад то, чего нет в справочнике компонентов
    component_exists = db.query(Component).get(component_id)
    if not component_exists:
        raise HTTPException(
            status_code=404,
            detail="Сначала создайте карточку компонента через /components, чтобы получить ID"
        )

    stock_item = db.query(Stock).filter(Stock.component_id == component_id).first()

    if stock_item:
        # Если деталь уже лежит на этом складе — плюсуем количество
        stock_item.actual_qty += quantity
        stock_item.location = location
    else:
        # Если приехала впервые — создаем запись об остатке
        stock_item = Stock(
            component_id=component_id,
            actual_qty=quantity,
            location=location
        )
        db.add(stock_item)

    db.commit()
    return {
        "status": "success",
        "component": component_exists.part_number,
        "new_qty": stock_item.actual_qty
    }