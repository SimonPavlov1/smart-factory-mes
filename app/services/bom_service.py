from sqlalchemy.orm import Session
from typing import List, Dict, Optional
from app.models.production import ProductBOM, BOMMapping
from app.models.inventory import Component
from app.logic.bom_parser import parse_with_context


class BOMMatchingService:
    """
    Единый сервис сопоставления спецификации (BOM).
    Интегрирует ручную загрузку и автоматический фоновый маппинг.
    """

    def __init__(self, db: Session):
        self.db = db
        # Категория для контекстного поиска (напр. "Резисторы")
        self.current_category: Optional[str] = "Other"

    @staticmethod
    def resolve_components(product_id: int, db: Session) -> int:
        """
        Метод для вызова из API (POST /resolve-bom).
        Пробегает по уже созданным записям в БД и пытается их 'разрешить'.
        """
        service = BOMMatchingService(db)

        # Запрашиваем только нераспознанные строки этого изделия
        items_to_resolve = db.query(ProductBOM).filter(
            ProductBOM.product_id == product_id,
            ProductBOM.is_resolved == False
        ).all()

        matched_count = 0
        for item in items_to_resolve:
            # Используем каскадный поиск (Маппинг -> Артикул -> Параметры)
            res_id = service._find_best_match(item.design_name)

            if res_id:
                item.resource_id = res_id
                item.is_resolved = True
                matched_count += 1
            else:
                # Если не нашли — регистрируем в таблице обучения
                service._ensure_mapping_exists(item.design_name)

        db.commit()
        return matched_count

    def process_bom_data(self, product_id: int, extracted_rows: List[Dict]) -> List[ProductBOM]:
        """Первичная обработка данных при загрузке (напр. из парсера)."""
        bom_items = []

        for row in extracted_rows:
            design_name = row.get("name", "").strip()
            designators = row.get("designators", "")
            quantity = row.get("quantity", 0)

            # Определение заголовка раздела (категории)
            if not designators and design_name and len(design_name.split()) < 3:
                self.current_category = design_name
                continue

            component_id = self._find_best_match(design_name)

            bom_item = ProductBOM(
                product_id=product_id,
                design_name=design_name,
                designators=designators,
                quantity=float(quantity),
                resource_id=component_id if component_id else None,
                resource_type="component",
                is_resolved=True if component_id else False
            )

            self.db.add(bom_item)
            bom_items.append(bom_item)

            if not component_id:
                self._ensure_mapping_exists(design_name)

        self.db.commit()
        return bom_items

    def _find_best_match(self, design_name: str) -> Optional[int]:
        """Каскадный поиск: от точного к параметрическому (аналоги)."""
        if not design_name:
            return None

        # УРОВЕНЬ А: ПРОВЕРКА ПАМЯТИ (BOMMapping)
        # Самый быстрый путь: если мы уже обучали систему этой строке
        mapping = self.db.query(BOMMapping).filter(BOMMapping.design_name == design_name).first()
        if mapping and mapping.component_id:
            return mapping.component_id

        # УРОВЕНЬ Б: ПОИСК ПО АРТИКУЛУ (Part Number)
        # Проверяем первое слово (MPN). Например, "RC0603..."
        clean_name = design_name.split()[0].strip()
        component = self.db.query(Component).filter(
            (Component.part_number == design_name) |
            (Component.part_number.ilike(f"{clean_name}%"))
        ).first()
        if component:
            return component.id

        # УРОВЕНЬ В: ПАРАМЕТРИЧЕСКИЙ ПОИСК (Поиск аналогов)
        # Если артикул не найден, парсим строку на номинал и корпус
        parsed = parse_with_context(design_name, self.current_category)

        # Если удалось вытащить 10кОм (10000) и 0603
        if parsed.get("value_numeric") and parsed.get("package"):
            # Ищем на складе ЛЮБОЙ компонент с такими же ТТХ
            # Здесь Yageo и Samsung встретятся, так как у них одинаковые параметры
            similar_component = self.db.query(Component).filter(
                Component.package == parsed["package"],
                Component.value_numeric == parsed["value_numeric"],
                # Категория помогает не перепутать резистор с конденсатором
                Component.category.ilike(f"%{parsed.get('category', self.current_category)}%")
            ).first()

            if similar_component:
                return similar_component.id

        return None

    def _ensure_mapping_exists(self, design_name: str):
        """Создает пустую запись для ручного маппинга закупщиком."""
        exists = self.db.query(BOMMapping).filter(BOMMapping.design_name == design_name).first()
        if not exists:
            new_mapping = BOMMapping(
                design_name=design_name,
                mapping_type="auto",
                is_verified=False
            )
            self.db.add(new_mapping)