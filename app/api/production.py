from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload
from typing import List, Optional
from pydantic import BaseModel

from app.database import get_db
from app.models.production import ProductType, ProductBOM, BOMMapping
from app.models.inventory import Component
from app.schemas.production import ProductCreateSchema, BOMItemCreate, BOMUploadResponse
from app.services.bom_service import BOMMatchingService

router = APIRouter(tags=["Производство (Production)"])


# --- Схемы данных ---

class BOMItemUpdate(BaseModel):
    design_name: str
    quantity: float
    designators: Optional[str] = None
    resource_id: Optional[int] = None
    resource_type: Optional[str] = "raw_string"
    is_resolved: Optional[bool] = False


class BOMManualItemSchema(BaseModel):
    design_name: str
    quantity: float
    designators: Optional[str] = None
    resource_id: Optional[int] = None
    resource_type: Optional[str] = "raw_string"
    is_resolved: Optional[bool] = False


class ProductUpdate(BaseModel):
    name: str
    drawing_number: Optional[str] = None
    revision: Optional[str] = "1.0"


# --- Вспомогательные функции ---

def create_product_recursive(data: ProductCreateSchema, db: Session):
    """Рекурсивное создание структуры изделия."""
    is_subassembly = not data.is_final

    new_product = ProductType(
        name=data.name,
        drawing_number=data.drawing_number,
        revision=data.version,
        is_subassembly=is_subassembly
    )
    db.add(new_product)
    db.flush()

    for item in data.components:
        current_id = item.resource_id
        if item.is_assembly and item.components:
            sub_data = ProductCreateSchema(
                name=item.design_name,
                drawing_number=f"SUB-{item.designators}-{new_product.drawing_number}",
                version=data.version,
                is_final=False,
                components=item.components
            )
            sub_product = create_product_recursive(sub_data, db)
            current_id = sub_product.id

        bom_entry = ProductBOM(
            product_id=new_product.id,
            designators=item.designators,
            design_name=item.design_name,
            quantity=item.quantity,
            resource_id=current_id if current_id != 0 else None,
            resource_type="subassembly" if item.is_assembly else "component",
            is_resolved=item.is_resolved
        )
        db.add(bom_entry)

    return new_product


# --- Эндпоинты ---

@router.get("/products")
def get_all_products(db: Session = Depends(get_db)):
    """Получение всех изделий с группировкой по категориям компонентов."""
    products = db.query(ProductType).options(selectinload(ProductType.components)).all()
    warehouse_cache = {c.id: c for c in db.query(Component).all()}
    products_cache = {p.id: p for p in db.query(ProductType).all()}

    result = []
    for product in products:
        grouped_sections = {}
        for item in product.components:
            item_data = {
                **item.__dict__,
                "resource": None
            }
            section = "Непривязанные компоненты"

            # СТРОГАЯ ПРОВЕРКА: Если тип "component", смотрим ТОЛЬКО на склад
            if item.resource_type == "component":
                comp = warehouse_cache.get(item.resource_id)
                if comp:
                    item_data["resource"] = {"id": comp.id, "name": comp.name, "part_number": comp.part_number}
                    section = getattr(comp, "category", "Прочие складские компоненты")

            # Если тип узел/изделие, смотрим ТОЛЬКО в каталог изделий
            elif item.resource_type in ["product", "subassembly"]:
                section = "Сборочные единицы (Узлы)"
                sub_prod = products_cache.get(item.resource_id)
                if sub_prod:
                    item_data["resource"] = {"id": sub_prod.id, "name": sub_prod.name,
                                             "drawing_number": sub_prod.drawing_number}

            grouped_sections.setdefault(section, []).append(item_data)

        sections = [{"name": name, "items": items} for name, items in grouped_sections.items()]
        sections.sort(key=lambda x: (x["name"] == "Непривязанные компоненты", x["name"]))

        result.append({
            "id": product.id,
            "name": product.name,
            "drawing_number": product.drawing_number,
            "revision": product.revision,
            "is_subassembly": product.is_subassembly,
            "sections": sections
        })
    return result


@router.post("/setup-product")
def setup_product(data: ProductCreateSchema, db: Session = Depends(get_db)):
    """Атомарная загрузка структуры изделия."""
    try:
        product = create_product_recursive(data, db)
        db.commit()
        db.refresh(product)
        return product
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/process-bom/{product_id}")
def process_manual_bom(product_id: int, items: List[BOMManualItemSchema], db: Session = Depends(get_db)):
    """Ручное добавление позиций в спецификацию с поддержкой готовых сборочных единиц."""
    processed_items = []
    items_to_match = []

    for item in items:
        # Если фронтенд передает уже готовую сборочную единицу или привязанный объект
        if item.resource_id is not None and item.resource_type in ["product", "subassembly", "component"]:
            bom_entry = ProductBOM(
                product_id=product_id,
                design_name=item.design_name.strip(),
                quantity=item.quantity,
                designators=item.designators.strip() if item.designators else None,
                resource_id=item.resource_id,
                resource_type=item.resource_type,
                is_resolved=True
            )
            db.add(bom_entry)
            db.flush()  # Генерируем ID для записи

            processed_items.append({
                "id": bom_entry.id,
                "design_name": bom_entry.design_name,
                "quantity": bom_entry.quantity,
                "designators": bom_entry.designators,
                "resource_id": bom_entry.resource_id,
                "resource_type": bom_entry.resource_type,
                "is_resolved": bom_entry.is_resolved
            })
        else:
            # Иначе отправляем строку на стандартный парсинг и умный поиск по складу
            items_to_match.append(item.dict())

    if items_to_match:
        matched = BOMMatchingService(db).process_bom_data(product_id, items_to_match)
        processed_items.extend(matched)

    db.commit()
    return {"product_id": product_id, "total_items": len(processed_items), "items": processed_items}


@router.post("/products/{product_id}/resolve-bom")
def resolve_bom(product_id: int, db: Session = Depends(get_db)):
    """Автоматическая привязка компонентов (умный подбор)."""
    count = BOMMatchingService.resolve_components(product_id, db)
    return {"status": "success", "matched_items": count}


@router.get("/bom-items/{item_id}/match-candidates")
def get_bom_match_candidates(item_id: int, db: Session = Depends(get_db)):
    """Возвращает кандидатов для ручной привязки строки BOM к складскому компоненту."""
    item = db.query(ProductBOM).get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")

    candidates = BOMMatchingService(db).find_match_candidates(item.design_name)
    return {"item_id": item.id, "design_name": item.design_name, "candidates": candidates}


@router.put("/bom-items/{item_id}")
def update_bom_item(item_id: int, data: BOMItemUpdate, db: Session = Depends(get_db)):
    """Обновление строки спецификации и гибкое переопределение её привязки."""
    item = db.query(ProductBOM).get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")

    # 1. Обновляем базовые текстовые и числовые поля
    item.design_name = data.design_name.strip()
    item.quantity = data.quantity
    item.designators = data.designators.strip() if data.designators else None

    # 2. Логика переопределения привязки (Проверяем, что прислал фронтенд)
    if data.resource_id is not None and data.resource_id != 0:
        # Если передан конкретный ID, жестко связываем с ним (неважно, компонент это или узел)
        item.resource_id = data.resource_id
        item.resource_type = data.resource_type if data.resource_type != "raw_string" else "component"
        item.is_resolved = True

        if item.resource_type == "component":
            mapping = db.query(BOMMapping).filter(BOMMapping.design_name == item.design_name).first()
            if not mapping:
                mapping = BOMMapping(design_name=item.design_name)
                db.add(mapping)
            mapping.component_id = item.resource_id
            mapping.mapping_type = "manual"
            mapping.is_verified = True
    else:
        # Если фронтенд прислал null, 0 или явно флаг снятия привязки
        item.resource_id = None
        item.resource_type = "raw_string"
        item.is_resolved = False

    db.commit()
    return {
        "status": "success",
        "detail": "Позиция успешно переопределена",
        "item": {
            "id": item.id,
            "resource_id": item.resource_id,
            "resource_type": item.resource_type,
            "is_resolved": item.is_resolved
        }
    }


@router.delete("/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db)):
    """Каскадное удаление изделия и всех его связей из каталога."""
    product = db.query(ProductType).get(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Изделие не найдено")

    db.query(ProductBOM).filter(ProductBOM.resource_id == product_id,
                                ProductBOM.resource_type.in_(["product", "subassembly"])).delete()
    db.query(ProductBOM).filter(ProductBOM.product_id == product_id).delete()
    db.delete(product)
    db.commit()
    return {"status": "success"}


@router.delete("/bom-items/{item_id}")
def delete_bom_item(item_id: int, db: Session = Depends(get_db)):
    """Удаление конкретного компонента или узла из состава изделия (строки спецификации)."""
    item = db.query(ProductBOM).get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция в спецификации не найдена")

    db.delete(item)
    db.commit()
    return {"status": "success", "detail": "Компонент успешно удален из состава"}
