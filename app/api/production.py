from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload
from typing import List

from app.database import get_db
from app.models.production import ProductType, ProductBOM
from app.models.inventory import Component
from app.schemas.production import ProductCreateSchema, BOMItemCreate, BOMUploadResponse
from app.services.bom_service import BOMMatchingService

router = APIRouter(prefix="/production", tags=["Производство (Production)"])

@router.get("/products")
def get_all_products(db: Session = Depends(get_db)):
    """
    Получить список всех изделий верхнего уровня.
    Подгружает вложенные компоненты для отображения дерева состава.
    """
    return db.query(ProductType).options(
        selectinload(ProductType.components).selectinload(ProductBOM.resource_component)
    ).filter(ProductType.is_subassembly == False).all()

@router.post("/process-bom/{product_id}", response_model=BOMUploadResponse)
def process_manual_bom(
    product_id: int,
    items: List[BOMItemCreate],
    db: Session = Depends(get_db)
):
    """
    УМНЫЙ РУЧНОЙ ВВОД:
    Принимает список строк (из Excel или PDF), распознает их через
    парсер и пытается найти соответствия на складе автоматически.
    """
    if not items:
        raise HTTPException(status_code=400, detail="Список компонентов пуст")

    # Превращаем Pydantic-модели в словари для сервиса сопоставления
    raw_data = [item.dict() for item in items]

    # Вызываем сервис, который мы писали ранее (он использует bom_parser)
    matching_service = BOMMatchingService(db)
    processed_items = matching_service.process_bom_data(product_id, raw_data)

    return {
        "product_id": product_id,
        "total_items": len(processed_items),
        "items": processed_items
    }

@router.post("/setup-product")
def setup_product(data: ProductCreateSchema, db: Session = Depends(get_db)):
    """
    Создание или полное обновление структуры изделия.
    Используется для фиксации финального состава платы после сопоставления.
    """
    try:
        # Здесь остается твоя рекурсивная логика создания (create_product_recursive)
        # Если она у тебя вынесена в функции, вызываем её здесь
        product = create_product_recursive(data, db)
        db.commit()
        return product
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Ошибка при создании изделия: {str(e)}")

# Вспомогательная функция (в идеале вынести в app/logic/product_logic.py)
def create_product_recursive(data, db: Session):
    # Твоя существующая логика рекурсивного обхода компонентов...
    # (Оставляем её без изменений, если она тебя устраивает)
    pass