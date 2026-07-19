from sqlalchemy.orm import Session
from app.models.inventory import Stock
from app.models.production import ProductBOM


def get_bom_requirements(product_id: int, qty: int, db: Session):
    """
    Рассчитывает суммарное количество всех базовых компонентов для заказа.
    Рекурсивно раскрывает сборочные единицы.
    """
    requirements = {}  # {component_id: total_qty}

    def _component_available(component_id: int):
        stock = db.query(Stock).filter(Stock.component_id == component_id).first()
        if not stock:
            return 0
        return max((stock.actual_qty or 0) - (stock.reserved_qty or 0), 0)

    def _allowed_component_ids(item: ProductBOM):
        result = []
        if item.resource_id:
            result.append(item.resource_id)
        for alternative in item.alternatives or []:
            if alternative.component_id not in result:
                result.append(alternative.component_id)
        return result

    def _add_requirement(component_id: int, needed_qty: float):
        if needed_qty <= 0:
            return
        requirements[component_id] = requirements.get(component_id, 0) + needed_qty

    def _allocate_component_requirement(item: ProductBOM, total_needed: float):
        allowed_ids = _allowed_component_ids(item)
        if not allowed_ids:
            return

        remaining = total_needed
        for component_id in allowed_ids:
            already_planned = requirements.get(component_id, 0)
            available = max(_component_available(component_id) - already_planned, 0)
            qty = min(remaining, available)
            if qty > 0:
                _add_requirement(component_id, qty)
                remaining -= qty
            if remaining <= 0:
                return

        _add_requirement(allowed_ids[0], remaining)

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

            if item_type == "component":
                _allocate_component_requirement(item, total_needed)

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
