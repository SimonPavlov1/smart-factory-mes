from sqlalchemy.orm import Session
from typing import List, Dict, Optional
from app.models.production import ProductBOM, BOMMapping
from app.models.inventory import Component
from app.logic.bom_parser import parse_with_context


class BOMMatchingService:
    """
    Сервис сопоставления спецификации (BOM).
    Отвечает за привязку текстовых строк из ПЭ3 к реальным ID компонентов на складе.
    """

    def __init__(self, db: Session):
        self.db = db
        # Храним категорию (напр. "Конденсаторы"), которая была встречена последней,
        # так как в ПЭ3 заголовок идет один раз для группы деталей.
        self.current_category: Optional[str] = "Other"

    def process_bom_data(self, product_id: int, extracted_rows: List[Dict]) -> List[ProductBOM]:
        """
        Массово обрабатывает список строк, полученных из парсера PDF/Excel.

        Args:
            product_id: ID изделия, для которого создается спецификация.
            extracted_rows: Список словарей с ключами 'designators', 'name', 'quantity'.
        """
        bom_items = []

        for row in extracted_rows:
            design_name = row.get("name", "").strip()
            designators = row.get("designators", "")
            quantity = row.get("quantity", 0)

            # 1. ОПРЕДЕЛЕНИЕ КОНТЕКСТА (Заголовок раздела)
            # Если в строке нет позиционных обозначений (C1, R1), но есть текст -
            # скорее всего, это заголовок раздела (напр. "Микросхемы").
            if not designators and design_name and len(design_name.split()) < 3:
                self.current_category = design_name
                continue

            # 2. ПОИСК СОПОСТАВЛЕНИЯ (Matching)
            # Пытаемся найти ID компонента через многоуровневый поиск
            component_id = self._find_best_match(design_name)

            # 3. СОХРАНЕНИЕ В СОСТАВ ИЗДЕЛИЯ
            # Создаем запись в ProductBOM. Если ресурс не найден, ставим 0 (нужна ручная привязка)
            bom_item = ProductBOM(
                product_id=product_id,
                design_name=design_name,
                designators=designators,
                quantity=float(quantity),
                resource_id=component_id if component_id else 0,
                resource_type="component",
                is_resolved=True if component_id else False  # Флаг "Готово" для фронтенда
            )

            self.db.add(bom_item)
            bom_items.append(bom_item)

            # 4. ОБУЧЕНИЕ СИСТЕМЫ
            # Если деталь не опознана, создаем "черновик" в таблице маппинга.
            # После того как закупщик один раз привяжет ее вручную, система запомнит выбор.
            if not component_id:
                self._ensure_mapping_exists(design_name)

        # Сохраняем все изменения в БД одним пакетом
        self.db.commit()
        return bom_items

    def _find_best_match(self, design_name: str) -> Optional[int]:
        """
        Каскадный поиск компонента (от точного к вероятностному).
        """

        # УРОВЕНЬ А: ПРОВЕРКА МАППИНГА (Память системы)
        # Ищем, не связывали ли мы ЭТУ ЖЕ строку с каким-то ID ранее
        mapping = self.db.query(BOMMapping).filter(
            BOMMapping.design_name == design_name
        ).first()
        if mapping and mapping.component_id:
            return mapping.component_id

        # УРОВЕНЬ Б: ПРЯМОЙ ПОИСК ПО PART NUMBER
        # Проверяем, не является ли всё название детали артикулом (MPN)
        component = self.db.query(Component).filter(
            Component.part_number == design_name
        ).first()
        if component:
            return component.id

        # УРОВЕНЬ В: ПАРАМЕТРИЧЕСКИЙ ПОИСК (Умный поиск)
        # Запускаем логику bom_parser для вычленения характеристик (10кОм, 0603 и т.д.)
        parsed = parse_with_context(design_name, self.current_category)

        # Если удалось вытащить и номинал, и корпус - ищем "функциональный аналог"
        if parsed.get("value_numeric") and parsed.get("package"):
            similar_component = self.db.query(Component).filter(
                Component.package == parsed["package"],
                Component.value_numeric == parsed["value_numeric"],
                # Используем поиск по вхождению категории (напр. "Resistors" в названии)
                Component.category.ilike(f"%{parsed['category']}%")
            ).first()

            if similar_component:
                return similar_component.id

        # Если все уровни не дали результата - возвращаем None
        return None

    def _ensure_mapping_exists(self, design_name: str):
        """
        Создает пустую запись в словаре маппинга.
        Это позволит закупщику увидеть "неразрешенные" позиции в интерфейсе.
        """
        exists = self.db.query(BOMMapping).filter(
            BOMMapping.design_name == design_name
        ).first()

        if not exists:
            new_mapping = BOMMapping(
                design_name=design_name,
                mapping_type="auto",  # Пометка, что создано автоматически парсером
                is_verified=False  # Требует подтверждения человеком
            )
            self.db.add(new_mapping)