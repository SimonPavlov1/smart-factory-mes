from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload
from typing import List

from app.database import get_db
from app.models.production import ProductType, ProductBOM
from app.schemas.production import ProductCreateSchema, BOMItemCreate, BOMUploadResponse

# Сохраняем твой старый сервис для метода process_manual_bom
from app.services.bom_service import BOMMatchingService as LegacyBOMService

router = APIRouter(tags=["Производство (Production)"])


def create_product_recursive(data: ProductCreateSchema, db: Session):
    """
    Рекурсивная функция для создания изделия и всей его иерархии (состава).

    Позволяет одной транзакцией создать главную плату и все вложенные узлы.
    """
    # 1. Создаем основную запись об изделии
    new_product = ProductType(
        name=data.name,
        drawing_number=data.drawing_number,
        revision=data.version,
        is_subassembly=not data.is_final
    )

    db.add(new_product)
    # Получаем ID без завершения транзакции
    db.flush()

    for item in data.components:
        current_resource_id = item.resource_id

        # 2. Обработка вложенных сборок (рекурсия)
        if item.is_assembly and item.components:
            sub_data = ProductCreateSchema(
                name=item.design_name,
                drawing_number=f"SUB-{item.designators}-{new_product.drawing_number}",
                version=data.version,
                is_final=False,
                components=item.components
            )
            sub_product = create_product_recursive(sub_data, db)
            current_resource_id = sub_product.id

        # 3. Создаем строку спецификации (BOM)
        bom_entry = ProductBOM(
            product_id=new_product.id,
            designators=item.designators,
            design_name=item.design_name,
            quantity=item.quantity,
            # Важно: записываем None вместо 0 для связей (Foreign Keys)
            resource_id=current_resource_id if current_resource_id != 0 else None,
            resource_type="product" if item.is_assembly else "component",
            is_resolved=item.is_resolved
        )
        db.add(bom_entry)

    return new_product


@router.get("/products")
def get_all_products(db: Session = Depends(get_db)):
    """Получить список всех изделий с подгрузкой состава (BOM)."""
    return db.query(ProductType).options(
        selectinload(ProductType.components)
    ).all()


@router.post("/setup-product")
def setup_product(data: ProductCreateSchema, db: Session = Depends(get_db)):
    """
    Атомарная загрузка структуры изделия.
    Либо создается все дерево с вложенными узлами, либо ничего.
    """
    try:
        product = create_product_recursive(data, db)
        db.commit()
        db.refresh(product)
        return product
    except Exception as e:
        db.rollback()
        print(f"Ошибка сохранения структуры: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка при создании изделия: {str(e)}")


@router.post("/process-bom/{product_id}", response_model=BOMUploadResponse)
def process_manual_bom(
        product_id: int,
        items: List[BOMItemCreate],
        db: Session = Depends(get_db)
):
    """Ручное добавление компонентов в существующий BOM через Legacy сервис."""
    if not items:
        raise HTTPException(status_code=400, detail="Список пуст")

    raw_data = [item.dict() for item in items]
    matching_service = LegacyBOMService(db)
    processed_items = matching_service.process_bom_data(product_id, raw_data)

    return {
        "product_id": product_id,
        "total_items": len(processed_items),
        "items": processed_items
    }


@router.post("/products/{product_id}/resolve-bom")
def resolve_bom(product_id: int, db: Session = Depends(get_db)):
    """
    Интеллектуальный запуск маппинга.
    Связывает строки BOM с реальным складом (ТМЦ).
    """
    # Локальный импорт внутри функции решает проблему циклической зависимости
    from app.services.matching_service import BOMMatchingService

    matched_count = BOMMatchingService.resolve_components(product_id, db)

    return {
        "status": "success",
        "product_id": product_id,
        "matched_items": matched_count,
        "message": f"Автоматически привязано компонентов: {matched_count}"
    }