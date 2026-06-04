from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload
from typing import List, Optional
from pydantic import BaseModel

from app.database import get_db
from app.models.production import ProductType, ProductBOM
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


class ProductUpdate(BaseModel):
    name: str
    drawing_number: Optional[str] = None
    revision: Optional[str] = "1.0"


# --- Вспомогательные функции ---

def create_product_recursive(data: ProductCreateSchema, db: Session):
    """Рекурсивное создание структуры изделия."""
    new_product = ProductType(
        name=data.name,
        drawing_number=data.drawing_number,
        revision=data.version,
        is_subassembly=not data.is_final
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
            resource_type="product" if item.is_assembly else "component",
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

            if item.resource_type == "component" or (item.resource_id in warehouse_cache):
                comp = warehouse_cache.get(item.resource_id)
                if comp:
                    item_data["resource"] = {"id": comp.id, "name": comp.name, "part_number": comp.part_number}
                    section = getattr(comp, "category", "Прочие складские компоненты")

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
            "id": product.id, "name": product.name, "drawing_number": product.drawing_number,
            "revision": product.revision, "sections": sections
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


@router.post("/process-bom/{product_id}", response_model=BOMUploadResponse)
def process_manual_bom(product_id: int, items: List[BOMItemCreate], db: Session = Depends(get_db)):
    """Ручное добавление позиций в спецификацию."""
    processed = BOMMatchingService(db).process_bom_data(product_id, [i.dict() for i in items])
    return {"product_id": product_id, "total_items": len(processed), "items": processed}


@router.post("/products/{product_id}/resolve-bom")
def resolve_bom(product_id: int, db: Session = Depends(get_db)):
    """Автоматическая привязка компонентов (умный подбор)."""
    count = BOMMatchingService.resolve_components(product_id, db)
    return {"status": "success", "matched_items": count}


@router.put("/bom-items/{item_id}")
def update_bom_item(item_id: int, data: BOMItemUpdate, db: Session = Depends(get_db)):
    """Обновление строки спецификации."""
    item = db.query(ProductBOM).get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")

    item.design_name = data.design_name.strip()
    item.quantity = data.quantity
    item.designators = data.designators.strip() if data.designators else None

    if data.resource_id is not None:
        item.resource_id, item.resource_type, item.is_resolved = data.resource_id, data.resource_type, True
    elif item.design_name != data.design_name:
        item.resource_id, item.resource_type, item.is_resolved = None, "raw_string", False

    db.commit()
    return {"status": "success"}


@router.delete("/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db)):
    """Каскадное удаление изделия и всех его связей."""
    product = db.query(ProductType).get(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Изделие не найдено")

    db.query(ProductBOM).filter(ProductBOM.resource_id == product_id,
                                ProductBOM.resource_type.in_(["product", "subassembly"])).delete()
    db.query(ProductBOM).filter(ProductBOM.product_id == product_id).delete()
    db.delete(product)
    db.commit()
    return {"status": "success"}