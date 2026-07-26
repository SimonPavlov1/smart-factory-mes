import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, selectinload
from typing import List, Optional
from pydantic import BaseModel, Field

from app.database import get_db
from app.models.production import ProductType, ProductBOM, BOMMapping, BOMItemAlternative
from app.models.inventory import Component, Stock
from app.schemas.production import ProductCreateSchema, BOMItemCreate, BOMUploadResponse
from app.services.bom_service import BOMMatchingService
from app.services.auth_service import require_roles

router = APIRouter(tags=["Производство (Production)"])
UPLOAD_ROOT = Path("uploads/products")


# --- Схемы данных ---

class BOMItemUpdate(BaseModel):
    design_name: str
    quantity: float
    designators: Optional[str] = None
    resource_id: Optional[int] = None
    resource_type: Optional[str] = "raw_string"
    item_type: Optional[str] = "component"
    parent_id: Optional[int] = None
    operation_role: Optional[str] = None
    sort_order: Optional[int] = 0
    is_resolved: Optional[bool] = False


class BOMManualItemSchema(BaseModel):
    design_name: str
    quantity: float
    designators: Optional[str] = None
    resource_id: Optional[int] = None
    resource_type: Optional[str] = "raw_string"
    item_type: Optional[str] = "component"
    parent_id: Optional[int] = None
    operation_role: Optional[str] = None
    sort_order: Optional[int] = 0
    is_resolved: Optional[bool] = False


class BOMAlternativeCreate(BaseModel):
    component_id: int
    note: Optional[str] = None
    is_primary: Optional[bool] = False


class ProductUpdate(BaseModel):
    name: str
    drawing_number: Optional[str] = None
    revision: Optional[str] = "1.0"
    test_checklist: Optional[list[str]] = None
    requires_preassembly_test: Optional[bool] = None
    factory_number_start: Optional[int] = Field(default=None, ge=1)


def _safe_filename(filename: str):
    cleaned = re.sub(r"[^A-Za-zА-Яа-я0-9._-]+", "_", filename).strip("._")
    return cleaned or "file"


def _product_file_payload(product_id: int, stored_name: str, original_name: str, content_type: str | None, file_type: str):
    return {
        "original_name": original_name,
        "stored_name": stored_name,
        "content_type": content_type,
        "file_type": file_type,
        "url": f"/products/{product_id}/files/{stored_name}",
    }


def _store_product_file(product_id: int, file: UploadFile, folder: str = "docs"):
    product_dir = UPLOAD_ROOT / str(product_id) / folder
    product_dir.mkdir(parents=True, exist_ok=True)
    original_name = _safe_filename(file.filename or "file")
    stored_name = f"{folder}_{uuid.uuid4().hex}_{original_name}"
    target = product_dir / stored_name

    with target.open("wb") as out:
        while chunk := file.file.read(1024 * 1024):
            out.write(chunk)

    return stored_name, original_name


# --- Вспомогательные функции ---

def create_product_recursive(data: ProductCreateSchema, db: Session):
    """Рекурсивное создание структуры изделия."""
    is_subassembly = not data.is_final

    new_product = ProductType(
        name=data.name,
        factory_number_start=data.factory_number_start,
        drawing_number=data.drawing_number,
        revision=data.version,
        is_subassembly=is_subassembly,
        test_checklist=[item.strip() for item in (data.test_checklist or []) if item.strip()],
        requires_preassembly_test=bool(data.requires_preassembly_test),
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
            parent_id=item.parent_id,
            designators=item.designators,
            design_name=item.design_name,
            quantity=item.quantity,
            resource_id=current_id if current_id != 0 else None,
            resource_type="subassembly" if item.is_assembly else "component",
            item_type="assembly" if item.is_assembly else item.item_type,
            operation_role=item.operation_role,
            sort_order=item.sort_order,
            is_resolved=item.is_resolved
        )
        db.add(bom_entry)

    return new_product


# --- Эндпоинты ---

def _alternative_payload(alternative: BOMItemAlternative, stock_cache: dict):
    component = alternative.component
    if not component:
        return None
    return {
        "id": alternative.id,
        "component_id": component.id,
        "name": component.name,
        "part_number": component.part_number,
        "category": component.category,
        "package": component.package,
        "value": component.value,
        "quantity": stock_cache.get(component.id, 0.0),
        "is_primary": alternative.is_primary,
        "note": alternative.note,
    }


def _item_payload(item: ProductBOM, warehouse_cache: dict, products_cache: dict, stock_cache: dict):
    item_type = item.item_type or ("assembly" if item.resource_type in ["product", "subassembly"] else "component")
    resource = None
    if item.resource_type == "component":
        comp = warehouse_cache.get(item.resource_id)
        if comp:
            resource = {
                "id": comp.id,
                "name": comp.name,
                "part_number": comp.part_number,
                "quantity": stock_cache.get(comp.id, 0.0),
            }
    elif item.resource_type in ["product", "subassembly"]:
        sub_prod = products_cache.get(item.resource_id)
        if sub_prod:
            resource = {"id": sub_prod.id, "name": sub_prod.name, "drawing_number": sub_prod.drawing_number}

    alternatives = []
    seen_components = set()
    primary_component = warehouse_cache.get(item.resource_id) if item.resource_type == "component" else None
    if primary_component:
        alternatives.append({
            "id": None,
            "component_id": primary_component.id,
            "name": primary_component.name,
            "part_number": primary_component.part_number,
            "category": primary_component.category,
            "package": primary_component.package,
            "value": primary_component.value,
            "quantity": stock_cache.get(primary_component.id, 0.0),
            "is_primary": True,
            "note": "Основная привязка",
        })
        seen_components.add(primary_component.id)
    for alternative in item.alternatives or []:
        payload = _alternative_payload(alternative, stock_cache)
        if payload and payload["component_id"] not in seen_components:
            alternatives.append(payload)
            seen_components.add(payload["component_id"])

    return {
        "id": item.id,
        "product_id": item.product_id,
        "parent_id": item.parent_id,
        "design_name": item.design_name,
        "designators": item.designators,
        "resource_id": item.resource_id,
        "resource_type": item.resource_type,
        "item_type": item_type,
        "operation_role": item.operation_role,
        "sort_order": item.sort_order or 0,
        "quantity": item.quantity,
        "is_resolved": item.is_resolved,
        "resource": resource,
        "alternatives": alternatives,
        "children": [],
    }


def _build_tree(items: list[ProductBOM], warehouse_cache: dict, products_cache: dict, stock_cache: dict):
    nodes = {_item.id: _item_payload(_item, warehouse_cache, products_cache, stock_cache) for _item in items}
    roots = []
    for item in sorted(items, key=lambda row: (row.parent_id or 0, row.sort_order or 0, row.id)):
        node = nodes[item.id]
        if item.parent_id and item.parent_id in nodes:
            nodes[item.parent_id]["children"].append(node)
        else:
            roots.append(node)
    return roots


def _ensure_primary_alternative(db: Session, item: ProductBOM, component_id: int):
    db.query(BOMItemAlternative).filter(BOMItemAlternative.bom_item_id == item.id).update({"is_primary": False})
    alternative = db.query(BOMItemAlternative).filter(
        BOMItemAlternative.bom_item_id == item.id,
        BOMItemAlternative.component_id == component_id
    ).first()
    if not alternative:
        alternative = BOMItemAlternative(bom_item_id=item.id, component_id=component_id)
        db.add(alternative)
    alternative.is_primary = True
    return alternative

@router.get("/products")
def get_all_products(
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer", "manager", "production", "warehouse", "assembler", "tester", "repair_engineer")),
):
    """Получение всех изделий с группировкой по категориям компонентов."""
    products = db.query(ProductType).options(
        selectinload(ProductType.components)
        .selectinload(ProductBOM.alternatives)
        .selectinload(BOMItemAlternative.component)
    ).all()
    warehouse_cache = {c.id: c for c in db.query(Component).all()}
    products_cache = {p.id: p for p in db.query(ProductType).all()}
    stock_cache = {stock.component_id: stock.actual_qty for stock in db.query(Stock).filter(Stock.component_id.isnot(None)).all()}

    result = []
    for product in products:
        grouped_sections = {}
        product_items = sorted(product.components, key=lambda row: (row.parent_id or 0, row.sort_order or 0, row.id))
        for item in product_items:
            if item.parent_id:
                continue
            item_type = item.item_type or ("assembly" if item.resource_type in ["product", "subassembly"] else "component")
            item_data = _item_payload(item, warehouse_cache, products_cache, stock_cache)
            section = "Непривязанные компоненты"

            # СТРОГАЯ ПРОВЕРКА: Если тип "component", смотрим ТОЛЬКО на склад
            if item_type == "operation":
                section = "Работы и операции"
                item_data["resource"] = {"name": item.operation_role or "Операция"}
            elif item.resource_type == "component":
                comp = warehouse_cache.get(item.resource_id)
                if comp:
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
            "photo_url": product.photo_url,
            "attachments": product.attachments or [],
            "test_checklist": product.test_checklist or [],
            "requires_preassembly_test": bool(product.requires_preassembly_test),
            "factory_number_start": product.factory_number_start or 1,
            "tree": _build_tree(product_items, warehouse_cache, products_cache, stock_cache),
            "sections": sections
        })
    return result


@router.post("/setup-product")
def setup_product(
    data: ProductCreateSchema,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer")),
):
    """Атомарная загрузка структуры изделия."""
    try:
        product = create_product_recursive(data, db)
        db.commit()
        db.refresh(product)
        return product
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/products/{product_id}")
def update_product(
    product_id: int,
    data: ProductUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer")),
):
    """Обновление паспорта изделия без изменения состава."""
    product = db.query(ProductType).filter(ProductType.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Изделие не найдено")

    product.name = data.name.strip()
    product.drawing_number = data.drawing_number
    product.revision = data.revision or "1.0"
    if data.test_checklist is not None:
        product.test_checklist = [item.strip() for item in data.test_checklist if item and item.strip()]
    if data.requires_preassembly_test is not None:
        product.requires_preassembly_test = bool(data.requires_preassembly_test)
    if data.factory_number_start is not None:
        product.factory_number_start = data.factory_number_start
    db.commit()
    db.refresh(product)
    return product


@router.post("/products/{product_id}/photo")
def upload_product_photo(
    product_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer")),
):
    """Загрузка основного фото изделия."""
    product = db.query(ProductType).filter(ProductType.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Изделие не найдено")
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Фото изделия должно быть изображением")

    stored_name, original_name = _store_product_file(product_id, file, folder="photo")
    payload = _product_file_payload(product_id, stored_name, original_name, file.content_type, "photo")
    product.photo_url = payload["url"]
    db.commit()
    return payload


@router.post("/products/{product_id}/attachments")
def upload_product_attachment(
    product_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer")),
):
    """Загрузка файлов КД, сборочных чертежей, состава и прочей документации изделия."""
    product = db.query(ProductType).filter(ProductType.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Изделие не найдено")

    stored_name, original_name = _store_product_file(product_id, file, folder="docs")
    attachment = _product_file_payload(product_id, stored_name, original_name, file.content_type, "attachment")
    attachments = list(product.attachments or [])
    attachments.append(attachment)
    product.attachments = attachments
    db.commit()
    return attachment


@router.get("/products/{product_id}/files/{stored_name}")
def download_product_file(
    product_id: int,
    stored_name: str,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer", "manager", "production", "warehouse", "assembler", "tester", "repair_engineer")),
):
    """Скачивание файла изделия."""
    product = db.query(ProductType).filter(ProductType.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Изделие не найдено")

    for folder in ["photo", "docs"]:
        path = UPLOAD_ROOT / str(product_id) / folder / stored_name
        if path.exists():
            return FileResponse(path)

    raise HTTPException(status_code=404, detail="Файл не найден")


@router.delete("/products/{product_id}/attachments/{stored_name}")
def delete_product_attachment(
    product_id: int,
    stored_name: str,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer")),
):
    """Удаление документа из паспорта изделия."""
    product = db.query(ProductType).filter(ProductType.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Изделие не найдено")

    attachments = [item for item in (product.attachments or []) if item.get("stored_name") != stored_name]
    if len(attachments) == len(product.attachments or []):
        raise HTTPException(status_code=404, detail="Документ не найден")

    path = UPLOAD_ROOT / str(product_id) / "docs" / stored_name
    if path.exists():
        path.unlink()
    product.attachments = attachments
    db.commit()
    return {"status": "success"}


@router.post("/process-bom/{product_id}")
def process_manual_bom(
    product_id: int,
    items: List[BOMManualItemSchema],
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer")),
):
    """Ручное добавление позиций в спецификацию с поддержкой готовых сборочных единиц."""
    processed_items = []
    items_to_match = []

    for item in items:
        # Если фронтенд передает уже готовую сборочную единицу или привязанный объект
        if item.resource_id is not None and item.resource_type in ["product", "subassembly", "component"]:
            bom_entry = ProductBOM(
                product_id=product_id,
                parent_id=item.parent_id,
                design_name=item.design_name.strip(),
                quantity=item.quantity,
                designators=item.designators.strip() if item.designators else None,
                resource_id=item.resource_id,
                resource_type=item.resource_type,
                item_type=item.item_type or ("assembly" if item.resource_type in ["product", "subassembly"] else "component"),
                operation_role=item.operation_role,
                sort_order=item.sort_order or 0,
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
                "item_type": bom_entry.item_type,
                "parent_id": bom_entry.parent_id,
                "operation_role": bom_entry.operation_role,
                "sort_order": bom_entry.sort_order,
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
def resolve_bom(
    product_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer")),
):
    """Автоматическая привязка компонентов (умный подбор)."""
    count = BOMMatchingService.resolve_components(product_id, db)
    return {"status": "success", "matched_items": count}


@router.get("/bom-items/{item_id}/match-candidates")
def get_bom_match_candidates(
    item_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer")),
):
    """Возвращает кандидатов для ручной привязки строки BOM к складскому компоненту."""
    item = db.query(ProductBOM).get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")

    candidates = BOMMatchingService(db).find_match_candidates(item.design_name)
    return {"item_id": item.id, "design_name": item.design_name, "candidates": candidates}


@router.put("/bom-items/{item_id}")
def update_bom_item(
    item_id: int,
    data: BOMItemUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer")),
):
    """Обновление строки спецификации и гибкое переопределение её привязки."""
    item = db.query(ProductBOM).get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")

    # 1. Обновляем базовые текстовые и числовые поля
    item.design_name = data.design_name.strip()
    item.quantity = data.quantity
    item.designators = data.designators.strip() if data.designators else None
    item.parent_id = data.parent_id
    item.item_type = data.item_type or item.item_type or "component"
    item.operation_role = data.operation_role
    item.sort_order = data.sort_order or 0

    if item.item_type == "operation":
        item.resource_id = None
        item.resource_type = "operation"
        item.is_resolved = True
        db.commit()
        return {
            "status": "success",
            "detail": "Операция успешно обновлена",
            "item": {
                "id": item.id,
                "resource_id": item.resource_id,
                "resource_type": item.resource_type,
                "item_type": item.item_type,
                "is_resolved": item.is_resolved
            }
        }

    # 2. Логика переопределения привязки (Проверяем, что прислал фронтенд)
    if data.resource_id is not None and data.resource_id != 0:
        # Если передан конкретный ID, жестко связываем с ним (неважно, компонент это или узел)
        item.resource_id = data.resource_id
        item.resource_type = data.resource_type if data.resource_type != "raw_string" else "component"
        item.is_resolved = True
        if item.resource_type == "component":
            _ensure_primary_alternative(db, item, item.resource_id)

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
        db.query(BOMItemAlternative).filter(BOMItemAlternative.bom_item_id == item.id).delete()

    db.commit()
    return {
        "status": "success",
        "detail": "Позиция успешно переопределена",
        "item": {
            "id": item.id,
            "resource_id": item.resource_id,
            "resource_type": item.resource_type,
            "item_type": item.item_type,
            "is_resolved": item.is_resolved
        }
    }


@router.post("/bom-items/{item_id}/alternatives")
def add_bom_item_alternative(
    item_id: int,
    data: BOMAlternativeCreate,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer")),
):
    """Добавляет разрешенный складской аналог к строке состава."""
    item = db.query(ProductBOM).get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    if (item.item_type or "component") != "component":
        raise HTTPException(status_code=400, detail="Аналоги можно добавлять только к покупным позициям")

    component = db.query(Component).get(data.component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Компонент склада не найден")

    existing = db.query(BOMItemAlternative).filter(
        BOMItemAlternative.bom_item_id == item.id,
        BOMItemAlternative.component_id == component.id
    ).first()
    if existing:
        if data.note is not None:
            existing.note = data.note
        if data.is_primary:
            item.resource_id = component.id
            item.resource_type = "component"
            item.is_resolved = True
            _ensure_primary_alternative(db, item, component.id)
        db.commit()
        return {"status": "success", "alternative_id": existing.id}

    if data.is_primary or not item.resource_id:
        item.resource_id = component.id
        item.resource_type = "component"
        item.is_resolved = True
        alternative = _ensure_primary_alternative(db, item, component.id)
        alternative.note = data.note
    else:
        alternative = BOMItemAlternative(
            bom_item_id=item.id,
            component_id=component.id,
            is_primary=False,
            note=data.note
        )
        db.add(alternative)

    db.commit()
    return {"status": "success", "alternative_id": alternative.id}


@router.put("/bom-items/{item_id}/alternatives/{component_id}/primary")
def set_bom_item_primary_alternative(
    item_id: int,
    component_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer")),
):
    """Делает аналог основной складской привязкой строки состава."""
    item = db.query(ProductBOM).get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    component = db.query(Component).get(component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Компонент склада не найден")

    item.resource_id = component.id
    item.resource_type = "component"
    item.item_type = "component"
    item.is_resolved = True
    _ensure_primary_alternative(db, item, component.id)
    db.commit()
    return {"status": "success"}


@router.delete("/bom-items/{item_id}/alternatives/{component_id}")
def delete_bom_item_alternative(
    item_id: int,
    component_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer")),
):
    """Удаляет разрешенный аналог. Если удаляется основной, строка остается без привязки."""
    item = db.query(ProductBOM).get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")

    db.query(BOMItemAlternative).filter(
        BOMItemAlternative.bom_item_id == item.id,
        BOMItemAlternative.component_id == component_id
    ).delete()
    if item.resource_type == "component" and item.resource_id == component_id:
        next_alt = db.query(BOMItemAlternative).filter(
            BOMItemAlternative.bom_item_id == item.id,
            BOMItemAlternative.component_id != component_id
        ).first()
        if next_alt:
            item.resource_id = next_alt.component_id
            item.resource_type = "component"
            item.is_resolved = True
            _ensure_primary_alternative(db, item, next_alt.component_id)
        else:
            item.resource_id = None
            item.resource_type = "raw_string"
            item.is_resolved = False

    db.commit()
    return {"status": "success"}


@router.delete("/products/{product_id}")
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer")),
):
    """Каскадное удаление изделия и всех его связей из каталога."""
    product = db.query(ProductType).get(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Изделие не найдено")

    linked_bom_ids = [
        row.id for row in db.query(ProductBOM.id).filter(
            ProductBOM.resource_id == product_id,
            ProductBOM.resource_type.in_(["product", "subassembly"])
        ).all()
    ]
    own_bom_ids = [row.id for row in db.query(ProductBOM.id).filter(ProductBOM.product_id == product_id).all()]
    bom_ids = linked_bom_ids + own_bom_ids
    if bom_ids:
        db.query(BOMItemAlternative).filter(BOMItemAlternative.bom_item_id.in_(bom_ids)).delete(synchronize_session=False)
    db.query(ProductBOM).filter(ProductBOM.id.in_(linked_bom_ids)).delete(synchronize_session=False)
    db.query(ProductBOM).filter(ProductBOM.id.in_(own_bom_ids)).delete(synchronize_session=False)
    db.delete(product)
    db.commit()
    return {"status": "success"}


@router.delete("/bom-items/{item_id}")
def delete_bom_item(
    item_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "engineer")),
):
    """Удаление конкретного компонента или узла из состава изделия (строки спецификации)."""
    item = db.query(ProductBOM).get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Позиция в спецификации не найдена")

    def collect_descendant_ids(parent_id: int):
        ids = []
        children = db.query(ProductBOM).filter(ProductBOM.parent_id == parent_id).all()
        for child in children:
            ids.append(child.id)
            ids.extend(collect_descendant_ids(child.id))
        return ids

    descendant_ids = collect_descendant_ids(item.id)
    delete_ids = descendant_ids + [item.id]
    db.query(BOMItemAlternative).filter(BOMItemAlternative.bom_item_id.in_(delete_ids)).delete(synchronize_session=False)
    if descendant_ids:
        db.query(ProductBOM).filter(ProductBOM.id.in_(descendant_ids)).delete(synchronize_session=False)
    db.delete(item)
    db.commit()
    return {"status": "success", "detail": "Компонент успешно удален из состава"}
