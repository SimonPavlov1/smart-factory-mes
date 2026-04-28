from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.services.bom_service import BOMMatchingService
from app.schemas.production import BOMUploadResponse, BOMItemCreate

router = APIRouter(prefix="/production", tags=["Производство"])


@router.post("/process-manual-bom/{product_id}", response_model=BOMUploadResponse)
def process_manual_bom(
        product_id: int,
        items: List[BOMItemCreate],  # Принимаем список, который заполнили руками в UI
        db: Session = Depends(get_db)
):
    """
    Принимает список компонентов, введенных вручную,
    и проводит их автоматическое сопоставление со складом.
    """
    if not items:
        raise HTTPException(status_code=400, detail="Список компонентов пуст")

    # Превращаем Pydantic-модели в словари для сервиса
    # Мы используем dict(), чтобы сервис мог работать с данными
    raw_data = [item.dict() for item in items]

    # Инициализируем сервис (в котором лежит наш каскадный поиск и парсер)
    matching_service = BOMMatchingService(db)

    # Обрабатываем данные и сохраняем в ProductBOM
    processed_items = matching_service.process_bom_data(product_id, raw_data)

    return {
        "product_id": product_id,
        "total_items": len(processed_items),
        "items": processed_items
    }