from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload
from typing import List

from app.database import get_db
from app.models.production import ProductType, ProductBOM
from app.schemas.production import ProductCreateSchema, BOMItemCreate, BOMUploadResponse

# Используем единый сервис для всех операций с BOM
from app.services.bom_service import BOMMatchingService

router = APIRouter(tags=["Производство (Production)"])


def create_product_recursive(data: ProductCreateSchema, db: Session):
    """
    Рекурсивная сборка изделия.

    Позволяет создать "дерево" изделия любой вложенности за один вызов.
    Если компонент помечен как сборка (is_assembly=True), функция вызывает
    саму себя, создает дочерний узел и привязывает его ID к родителю.
    """
    # 1. Создаем "голову" изделия (Плата или Узел)
    new_product = ProductType(
        name=data.name,
        drawing_number=data.drawing_number,
        revision=data.version,
        is_subassembly=not data.is_final
    )

    db.add(new_product)
    # flush() синхронизирует объект с БД, чтобы получить ID, но не закрывает транзакцию.
    # Это позволяет откатить всё создание целиком при ошибке во вложенных узлах.
    db.flush()

    for item in data.components:
        current_resource_id = item.resource_id

        # 2. Логика рекурсии: если внутри BOM сидит другая сборка
        if item.is_assembly and item.components:
            sub_data = ProductCreateSchema(
                name=item.design_name,
                drawing_number=f"SUB-{item.designators}-{new_product.drawing_number}",
                version=data.version,
                is_final=False,
                components=item.components
            )
            # Рекурсивный вызов для создания вложенного узла
            sub_product = create_product_recursive(sub_data, db)
            current_resource_id = sub_product.id

        # 3. Сохранение строки спецификации (BOM)
        bom_entry = ProductBOM(
            product_id=new_product.id,
            designators=item.designators,
            design_name=item.design_name,
            quantity=item.quantity,
            # Если resource_id пришел как 0, в базу пишем None для корректной работы FK
            resource_id=current_resource_id if current_resource_id != 0 else None,
            resource_type="product" if item.is_assembly else "component",
            is_resolved=item.is_resolved
        )
        db.add(bom_entry)

    return new_product


@router.get("/products")
def get_all_products(db: Session = Depends(get_db)):
    """
    Получение списка всех изделий.
    Использует selectinload для 'жадной' загрузки BOM, чтобы избежать проблемы N+1 запросов.
    """
    return db.query(ProductType).options(
        selectinload(ProductType.components)
    ).all()


@router.post("/setup-product")
def setup_product(data: ProductCreateSchema, db: Session = Depends(get_db)):
    """
    Атомарная загрузка структуры изделия.
    Либо создается все дерево (со всеми вложенными платами), либо ничего.
    """
    try:
        product = create_product_recursive(data, db)
        db.commit()  # Завершаем транзакцию только если всё создалось успешно
        db.refresh(product)
        return product
    except Exception as e:
        db.rollback()  # Отмена всех изменений при любой ошибке
        print(f"Ошибка в транзакции: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка создания структуры: {str(e)}")


@router.post("/process-bom/{product_id}", response_model=BOMUploadResponse)
def process_manual_bom(
        product_id: int,
        items: List[BOMItemCreate],
        db: Session = Depends(get_db)
):
    """
    Ручное добавление позиций в уже существующую плату.
    Сразу пытается сопоставить добавленные строки со складом.
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


@router.post("/products/{product_id}/resolve-bom")
def resolve_bom(product_id: int, db: Session = Depends(get_db)):
    """
    Интеллектуальный запуск маппинга.

    Пробегает по всем неразрешенным строкам изделия и пытается найти
    их в справочнике ТМЦ или в таблице уже заученных сопоставлений (BOMMapping).
    """
    # Вызываем статический метод из нашего единого сервиса
    matched_count = BOMMatchingService.resolve_components(product_id, db)

    return {
        "status": "success",
        "product_id": product_id,
        "matched_items": matched_count,
        "message": f"Автоматически привязано компонентов: {matched_count}"
    }