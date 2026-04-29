from sqlalchemy.orm import Session
from typing import List, Dict, Optional
from app.models.production import ProductBOM, BOMMapping
from app.models.inventory import Component
from app.logic.bom_parser import parse_with_context


class BOMMatchingService:
    def __init__(self, db: Session):
        self.db = db
        self.current_category: Optional[str] = "Other"

    @staticmethod
    def resolve_components(product_id: int, db: Session) -> int:
        service = BOMMatchingService(db)
        items_to_resolve = db.query(ProductBOM).filter(
            ProductBOM.product_id == product_id,
            ProductBOM.is_resolved == False
        ).all()

        matched_count = 0
        for item in items_to_resolve:
            res_id = service._find_best_match(item.design_name)
            if res_id:
                item.resource_id = res_id
                item.is_resolved = True
                matched_count += 1
            else:
                service._ensure_mapping_exists(item.design_name)

        db.commit()
        return matched_count

    def _find_best_match(self, design_name: str) -> Optional[int]:
        """Каскадный поиск: Маппинг -> Артикул -> Параметры."""
        if not design_name:
            return None

        # --- УРОВЕНЬ А: ПРОВЕРКА ПАМЯТИ ---
        mapping = self.db.query(BOMMapping).filter(BOMMapping.design_name == design_name).first()
        if mapping and mapping.component_id:
            return mapping.component_id

        # --- УРОВЕНЬ Б: ПОИСК ПО АРТИКУЛУ ---
        clean_name = design_name.split()[0].strip()
        component = self.db.query(Component).filter(
            (Component.part_number == design_name) |
            (Component.part_number.ilike(f"{clean_name}%"))
        ).first()
        if component:
            return component.id

        # --- УРОВЕНЬ В: ПАРАМЕТРИЧЕСКИЙ ПОИСК (Аналоги) ---
        # 1. Пред-обработка строки для парсера (RU -> EN)
        norm_name = design_name.replace("кОм", "k").replace("к", "k").replace("мкФ", "uF")

        parsed = parse_with_context(norm_name, self.current_category)

        # Логируем для отладки
        print(f"DEBUG: Search for: {norm_name} | Parsed: {parsed}")

        if parsed.get("package"):
            # Базовый запрос по корпусу и категории
            query = self.db.query(Component).filter(
                Component.package.ilike(parsed["package"]),
                Component.category.ilike(f"%{self.current_category}%")
            )

            # А. Ищем по точному числу (самый надежный способ для аналогов)
            if parsed.get("value_numeric"):
                similar = query.filter(Component.value_numeric == parsed["value_numeric"]).first()
                if similar:
                    return similar.id

            # Б. Резервный поиск по строке (если число не распарсилось, ищем "10k" в поле value)
            raw_val = parsed.get("value")  # Предположим, парсер вернул строку
            if raw_val:
                similar = query.filter(Component.value.ilike(f"%{raw_val}%")).first()
                if similar:
                    return similar.id

        return None

    def process_bom_data(self, product_id: int, extracted_rows: List[Dict]) -> List[ProductBOM]:
        bom_items = []
        for row in extracted_rows:
            design_name = row.get("name", "").strip()
            designators = row.get("designators", "")
            quantity = row.get("quantity", 0)

            # Определение категории по заголовку (если нет десигнаторов)
            if not designators and design_name and len(design_name.split()) < 3:
                self.current_category = design_name
                continue

            component_id = self._find_best_match(design_name)

            bom_item = ProductBOM(
                product_id=product_id,
                design_name=design_name,
                designators=designators,
                quantity=float(quantity),
                resource_id=component_id,
                resource_type="component",
                is_resolved=True if component_id else False
            )
            self.db.add(bom_item)
            bom_items.append(bom_item)

            if not component_id:
                self._ensure_mapping_exists(design_name)

        self.db.commit()
        return bom_items

    def _ensure_mapping_exists(self, design_name: str):
        exists = self.db.query(BOMMapping).filter(BOMMapping.design_name == design_name).first()
        if not exists:
            new_mapping = BOMMapping(
                design_name=design_name,
                mapping_type="auto",
                is_verified=False
            )
            self.db.add(new_mapping)