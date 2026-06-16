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

        for item in bom_items:
            # Считаем количество с учетом текущего множителя (вложенности)
            total_needed = item.quantity * multiplier

            if item.resource_type == "component":
                # Это базовый компонент, добавляем его в общую корзину
                requirements[item.resource_id] = requirements.get(item.resource_id, 0) + total_needed

            elif item.resource_type in ["product", "subassembly"] and item.resource_id:
                # Это узел, идем внутрь него рекурсивно
                _resolve(item.resource_id, total_needed)

    # Запускаем рекурсию
    _resolve(product_id, qty)

    # Превращаем словарь {id: qty} в список словарей для сервиса резервирования
    return [{"component_id": c_id, "qty": q} for c_id, q in requirements.items()]