from sqlalchemy.orm import Session
from app.models.production import ProductComponent
from app.models.inventory import Component


class BOMMatchingService:
    """
    Сервис для автоматической привязки позиций из BOM (состава изделия)
    к реальным записям в справочнике ТМЦ (складе).
    """

    @staticmethod
    def resolve_components(product_id: int, db: Session) -> int:
        """
        Проходит по списку компонентов изделия и сопоставляет их со складом.

        Алгоритм:
        1. Извлекает все записи ProductComponent, которые еще не привязаны (is_resolved=False).
        2. Очищает поле design_name (берет первое слово как основной артикул).
        3. Ищет частичное совпадение в справочнике ТМЦ по part_number.
        4. В случае успеха — связывает запись с resource_id и ставит флаг готовности.
        """
        # Получаем список нерешенных задач для конкретного изделия
        unresolved_items = db.query(ProductComponent).filter(
            ProductComponent.product_id == product_id,
            ProductComponent.is_resolved == False
        ).all()

        matched_count = 0

        for item in unresolved_items:
            # Предобработка: берем артикул (напр. 'RC0603FR-074K99L' из 'RC0603FR-074K99L Yageo')
            # Очищаем от пробелов и приводим к верхнему регистру для поиска
            raw_design_name = item.design_name or ""
            search_query = raw_design_name.split()[0].strip().upper()

            if not search_query:
                continue

            # Поиск в справочнике ТМЦ. Используем ilike для регистронезависимости
            match = db.query(Component).filter(
                Component.part_number.ilike(f"{search_query}%")
            ).first()

            if match:
                # Привязываем найденный компонент
                item.resource_id = match.id
                item.is_resolved = True
                matched_count += 1

        # Сохраняем все изменения в БД одним транзакционным блоком
        db.commit()
        return matched_count