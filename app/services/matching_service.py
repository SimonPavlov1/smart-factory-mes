from sqlalchemy.orm import Session
# Мы заменили ProductComponent на ProductBOM, так как это имя в твоих моделях
from app.models.production import ProductBOM
from app.models.inventory import Component


class BOMMatchingService:
    """
    Интеллектуальный сервис сопоставления состава изделия со складом.
    Связывает текстовые записи в BOM с реальными ID компонентов ТМЦ.
    """

    @staticmethod
    def resolve_components(product_id: int, db: Session) -> int:
        """
        Ищет совпадения для всех нераспознанных позиций в конкретном изделии.

        Логика:
        1. Находит строки ProductBOM, где is_resolved == False.
        2. Извлекает артикул (первое слово) из design_name.
        3. Ищет этот артикул в справочнике ТМЦ.
        """

        # Запрашиваем из базы только нераспознанные компоненты этого изделия
        unresolved_items = db.query(ProductBOM).filter(
            ProductBOM.product_id == product_id,
            ProductBOM.is_resolved == False
        ).all()

        matched_count = 0

        for item in unresolved_items:
            # Пропускаем, если название не указано
            if not item.design_name:
                continue

            # Берем первое слово (артикул) и чистим от лишнего
            # "RC0603FR-074K99L YAGEO" -> "RC0603FR-074K99L"
            search_query = item.design_name.split()[0].strip()

            # Ищем в справочнике ТМЦ (регистронезависимо)
            match = db.query(Component).filter(
                Component.part_number.ilike(f"{search_query}%")
            ).first()

            if match:
                # Если нашли — привязываем ID из склада и ставим флаг готовности
                item.resource_id = match.id
                item.is_resolved = True
                matched_count += 1

        # Сохраняем все изменения в базе
        db.commit()

        return matched_count