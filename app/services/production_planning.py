from sqlalchemy.orm import Session
from app.models.production import ProductBOM


def get_bom_requirements(product_id: int, qty: int, db: Session):
    """
    Рассчитывает суммарное количество всех базовых компонентов для заказа.
    Рекурсивно раскрывает сборочные единицы.
    """
    requirements = {}  # {component_id: total_qty}

    def _resolve(p_id, multiplier):
        # Получаем все позиции BOM для текущего изделия/узла
        bom_items = db.query(ProductBOM).filter(ProductBOM.product_id == p_id).all()
        children_by_parent = {}
        for bom_item in bom_items:
            if bom_item.parent_id:
                children_by_parent.setdefault(bom_item.parent_id, []).append(bom_item)

        def _resolve_item(item, item_multiplier):
            item_type = item.item_type or ("assembly" if item.resource_type in ["product", "subassembly"] else "component")
            if item_type == "operation":
                return

            total_needed = item.quantity * item_multiplier

            if item_type == "component" and item.resource_type == "component" and item.resource_id:
                requirements[item.resource_id] = requirements.get(item.resource_id, 0) + total_needed

            elif item_type == "assembly":
                if item.resource_type in ["product", "subassembly"] and item.resource_id:
                    _resolve(item.resource_id, total_needed)
                for child in children_by_parent.get(item.id, []):
                    _resolve_item(child, total_needed)

        for item in bom_items:
            if item.parent_id:
                continue
            _resolve_item(item, multiplier)

    # Запускаем рекурсию
    _resolve(product_id, qty)

    # Превращаем словарь {id: qty} в список словарей для сервиса резервирования
    return [{"component_id": c_id, "qty": q} for c_id, q in requirements.items()]
