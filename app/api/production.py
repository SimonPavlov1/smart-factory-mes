from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload
from typing import List

from app.database import get_db
from app.models.production import ProductType, ProductBOM
from app.schemas.production import ProductCreateSchema, BOMItemCreate, BOMUploadResponse
from app.services.bom_service import BOMMatchingService

router = APIRouter(tags=["Производство (Production)"])


def create_product_recursive(data: ProductCreateSchema, db: Session):
    """
    Рекурсивная функция для создания изделия и всей его иерархии (состава).

    Логика:
    1. Создает запись в таблице изделий (ProductType).
    2. Если в составе есть вложенный узел (is_assembly=True), функция вызывает
       саму себя, чтобы сначала создать этот узел и получить его ID.
    3. Создает записи в таблице состава (ProductBOM), связывая их с изделием.
    """

    # Создаем основную запись об изделии (плате или блоке)
    new_product = ProductType(
        name=data.name,
        drawing_number=data.drawing_number,
        revision=data.version,
        # Если изделие не финальное, значит это промежуточный узел (subassembly)
        is_subassembly=not data.is_final
    )

    db.add(new_product)
    # flush() отправляет данные в БД и получает сгенерированный ID,
    # но не завершает транзакцию (позволяет откатиться при ошибке)
    db.flush()

    for item in data.components:
        current_resource_id = item.resource_id

        # Обработка вложенных сборок (рекурсия)
        if item.is_assembly and item.components:
            # Преобразуем данные компонента в схему изделия для рекурсивного вызова
            sub_data = ProductCreateSchema(
                name=item.design_name,
                drawing_number=f"SUB-{item.designators}-{new_product.drawing_number}",
                version=data.version,
                is_final=False,
                components=item.components
            )
            sub_product = create_product_recursive(sub_data, db)
            # ID созданного узла становится resource_id для текущей строки состава
            current_resource_id = sub_product.id

        # Создаем строку спецификации (BOM)
        bom_entry = ProductBOM(
            product_id=new_product.id,
            designators=item.designators,
            design_name=item.design_name,
            quantity=item.quantity,
            # В БД записываем None вместо 0 для корректной работы связей (FK)
            resource_id=current_resource_id if current_resource_id != 0 else None,
            resource_type="product" if item.is_assembly else "component",
            is_resolved=item.is_resolved
        )
        db.add(bom_entry)

    return new_product


@router.get("/products")
def get_all_products(db: Session = Depends(get_db)):
    """
    Получить список всех изделий.
    Использует selectinload для быстрой подгрузки состава одним запросом.
    """
    return db.query(ProductType).options(
        selectinload(ProductType.components)
    ).all()


@router.post("/setup-product")
def setup_product(data: ProductCreateSchema, db: Session = Depends(get_db)):
    """
    Создание изделия "с нуля" или загрузка полной структуры.
    Обеспечивает атомарность: либо создается всё дерево, либо ничего.
    """
    try:
        product = create_product_recursive(data, db)
        db.commit()  # Сохраняем все изменения в базе данных
        db.refresh(product)  # Загружаем актуальное состояние (с ID и связями)
        return product
    except Exception as e:
        db.rollback()  # Отменяем всё, если произошла любая ошибка
        print(f"Ошибка сохранения в БД: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка при создании изделия: {str(e)}")


@router.post("/process-bom/{product_id}", response_model=BOMUploadResponse)
def process_manual_bom(
        product_id: int,
        items: List[BOMItemCreate],
        db: Session = Depends(get_db)
):
    """
    Добавление компонентов в уже существующее изделие
    с автоматическим поиском аналогов на складе.
    """
    if not items:
        raise HTTPException(status_code=400, detail="Список компонентов пуст")

    raw_data = [item.dict() for item in items]
    matching_service = BOMMatchingService(db)
    processed_items = matching_service.process_bom_data(product_id, raw_data)

    return {
        "product_id": product_id,
        "total_items": len(processed_items),
        "items": processed_items
    }