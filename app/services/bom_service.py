import re
from difflib import SequenceMatcher
from sqlalchemy import or_
from sqlalchemy.orm import Session
from typing import List, Dict, Optional
from app.models.production import ProductBOM, BOMMapping
from app.models.inventory import Component
from app.logic.bom_parser import parse_with_context


class BOMMatchingService:
    AUTO_MATCH_SCORE = 0.82
    CANDIDATE_SCORE = 0.45

    def __init__(self, db: Session):
        self.db = db
        self.current_category: Optional[str] = "Other"

    @staticmethod
    def _normalize_text(value: Optional[str]) -> str:
        if not value:
            return ""

        normalized = value.lower()
        replacements = {
            "ё": "е",
            "×": "x",
            "кoм": "ком",
            "kohm": "k",
            "ком": "k",
            "ом": "r",
            "мкф": "uf",
            "мк": "u",
            "нф": "nf",
            "пф": "pf",
            "в": "v",
        }
        for old, new in replacements.items():
            normalized = normalized.replace(old, new)

        return re.sub(r"[^a-zа-я0-9.%]+", "", normalized)

    @staticmethod
    def _category_aliases(category: Optional[str]) -> List[str]:
        text = (category or "").lower()
        aliases = []
        groups = {
            "capacitor": ["capacitor", "capacitors", "конденсатор", "конденсаторы"],
            "resistor": ["resistor", "resistors", "резистор", "резисторы"],
            "diode": ["diode", "diodes", "диод", "диоды"],
            "transistor": ["transistor", "transistors", "транзистор", "транзисторы"],
            "ic": ["integrated", "микросхема", "микросхемы", "ic"],
        }
        for values in groups.values():
            if any(alias in text for alias in values):
                aliases.extend(values)
        return aliases

    @staticmethod
    def _numbers_equal(left, right, tolerance=0.02) -> bool:
        if left is None or right is None:
            return False
        if left == right:
            return True
        base = max(abs(left), abs(right), 1)
        return abs(left - right) / base <= tolerance

    def _parse_component_like_bom(self, component: Component):
        text = " ".join(filter(None, [
            component.category,
            component.name,
            component.part_number,
            component.value,
            component.package,
            f"{component.voltage}V" if component.voltage else None,
        ]))
        return parse_with_context(text, component.category)

    def _component_payload(self, component: Component, score: float, reason: str):
        return {
            "id": component.id,
            "name": component.name,
            "part_number": component.part_number,
            "category": component.category,
            "package": component.package,
            "value": component.value,
            "voltage": component.voltage,
            "score": round(score, 3),
            "reason": reason,
        }

    def _prefilter_components(self, design_name: str, parsed: Dict, limit: int = 300):
        filters = []
        words = [word for word in re.split(r"\s+", design_name.strip()) if len(word) >= 2]

        for word in words[:6]:
            pattern = f"%{word}%"
            filters.append(Component.part_number.ilike(pattern))
            filters.append(Component.name.ilike(pattern))

        if parsed.get("package"):
            filters.append(Component.package.ilike(parsed["package"]))

        if parsed.get("value_numeric") is not None:
            value = parsed["value_numeric"]
            delta = max(abs(value) * 0.02, 1e-12)
            filters.append(Component.value_numeric.between(value - delta, value + delta))

        for alias in self._category_aliases(parsed.get("category") or self.current_category):
            pattern = f"%{alias}%"
            filters.append(Component.category.ilike(pattern))
            filters.append(Component.name.ilike(pattern))

        if not filters:
            return self.db.query(Component).order_by(Component.id.desc()).limit(limit).all()

        return (
            self.db.query(Component)
            .filter(or_(*filters))
            .order_by(Component.part_number, Component.name)
            .limit(limit)
            .all()
        )

    def _score_component(self, design_name: str, parsed: Dict, component: Component):
        normalized_design = self._normalize_text(design_name)
        normalized_part = self._normalize_text(component.part_number)
        normalized_name = self._normalize_text(component.name)

        if normalized_part:
            if normalized_design == normalized_part:
                return 0.98, "part_number_exact"
            if normalized_part in normalized_design:
                return 0.92, "part_number_in_text"

        score = 0.0
        reasons = []

        component_parsed = self._parse_component_like_bom(component)

        if parsed.get("package") and component.package:
            if parsed["package"].lower() == component.package.lower():
                score += 0.25
                reasons.append("package")

        if parsed.get("value_numeric"):
            component_value = component.value_numeric or component_parsed.get("value_numeric")
            if self._numbers_equal(parsed["value_numeric"], component_value):
                score += 0.35
                reasons.append("value")
            elif component.value and self._normalize_text(component.value) in normalized_design:
                score += 0.25
                reasons.append("value_text")

        if parsed.get("voltage") and component.voltage:
            if component.voltage >= parsed["voltage"]:
                score += 0.10
                reasons.append("voltage")

        category_aliases = self._category_aliases(parsed.get("category") or self.current_category)
        component_category = (component.category or "").lower()
        component_name = (component.name or "").lower()
        if category_aliases and any(alias in component_category or alias in component_name for alias in category_aliases):
            score += 0.15
            reasons.append("category")

        text_ratio = max(
            SequenceMatcher(None, normalized_design, normalized_name).ratio() if normalized_name else 0,
            SequenceMatcher(None, normalized_design, normalized_part).ratio() if normalized_part else 0,
        )
        if text_ratio >= 0.45:
            score += min(text_ratio * 0.15, 0.15)
            reasons.append("text")

        return min(score, 0.99), "+".join(reasons) if reasons else "weak_text"

    @staticmethod
    def resolve_components(product_id: int, db: Session) -> int:
        service = BOMMatchingService(db)
        items_to_resolve = db.query(ProductBOM).filter(
            ProductBOM.product_id == product_id,
            ProductBOM.is_resolved == False
        ).all()

        matched_count = 0
        for item in items_to_resolve:
            match = service.find_best_match(item.design_name)
            if match:
                item.resource_id = match["component_id"]
                item.resource_type = "component"
                item.is_resolved = True
                matched_count += 1
            else:
                service._ensure_mapping_exists(item.design_name)

        db.commit()
        return matched_count

    def find_match_candidates(self, design_name: str, limit: int = 5) -> List[Dict]:
        if not design_name:
            return []

        mapping = self.db.query(BOMMapping).filter(BOMMapping.design_name.ilike(design_name)).first()
        if mapping and mapping.component_id:
            component = self.db.query(Component).filter(Component.id == mapping.component_id).first()
            if component:
                return [self._component_payload(component, 1.0, "verified_mapping" if mapping.is_verified else "mapping")]

        normalized = self._normalize_text(design_name)
        category_context = self.current_category if self.current_category != "Other" else design_name
        parsed = parse_with_context(design_name, category_context)

        candidates = []
        for component in self._prefilter_components(design_name, parsed):
            score, reason = self._score_component(design_name, parsed, component)

            normalized_part = self._normalize_text(component.part_number)
            if normalized_part and normalized_part in normalized:
                score = max(score, 0.92)
                reason = "part_number_in_text"

            if score >= self.CANDIDATE_SCORE:
                candidates.append(self._component_payload(component, score, reason))

        candidates.sort(key=lambda item: item["score"], reverse=True)
        return candidates[:limit]

    def find_best_match(self, design_name: str) -> Optional[Dict]:
        candidates = self.find_match_candidates(design_name, limit=1)
        if not candidates:
            return None

        best = candidates[0]
        if best["score"] < self.AUTO_MATCH_SCORE:
            return None

        return {"component_id": best["id"], "score": best["score"], "reason": best["reason"]}

    def _find_best_match(self, design_name: str) -> Optional[int]:
        """Каскадный поиск: Маппинг -> Артикул -> Параметры."""
        match = self.find_best_match(design_name)
        return match["component_id"] if match else None

    def process_bom_data(self, product_id: int, extracted_rows: List[Dict]) -> List[ProductBOM]:
        bom_items = []
        for row in extracted_rows:
            # ИСПРАВЛЕНО: Сначала пытаемся взять 'design_name' (как шлет фронтенд),
            # если его нет — берем 'name' (для обратной совместимости с парсером файлов)
            design_name = row.get("design_name") or row.get("name") or ""
            design_name = design_name.strip()

            designators = row.get("designators", "")
            quantity = row.get("quantity", 0)

            # Определение категории по заголовку (если нет десигнаторов)
            if not designators and design_name and len(design_name.split()) < 3:
                self.current_category = design_name
                continue

            # Если имя строки оказалось совсем пустым, не плодим призраков
            if not design_name:
                continue

            # Пытаемся найти автоподбором на складе
            match = self.find_best_match(design_name)
            component_id = match["component_id"] if match else None

            # Получаем тип ресурса, который прислал фронтенд (опционально)
            incoming_resource_type = row.get("resource_type", "raw_string")

            # Формируем запись для базы данных спецификации
            bom_item = ProductBOM(
                product_id=product_id,
                design_name=design_name,  # ИМЯ ТЕПЕРЬ СОХРАНИТСЯ НАВСЕГДА!
                designators=designators,
                quantity=float(quantity),
                resource_id=component_id,  # ID склада привяжется, только если find_best_match его нашел

                # Если компонент успешно сопоставлен со складом — пишем 'component'.
                # Если совпадений нет — сохраняем тот тип, который передал фронтенд ('raw_string')
                resource_type="component" if component_id else incoming_resource_type,
                is_resolved=True if component_id else False
            )
            self.db.add(bom_item)
            bom_items.append(bom_item)

            # Если складской аналог не найден, фиксируем строку в таблице неразрешенных маппингов
            if component_id:
                self._remember_mapping(design_name, component_id, mapping_type="auto", is_verified=False)
            else:
                self._ensure_mapping_exists(design_name)

        self.db.commit()
        return bom_items

    def _remember_mapping(self, design_name: str, component_id: int, mapping_type: str, is_verified: bool):
        mapping = self.db.query(BOMMapping).filter(BOMMapping.design_name == design_name).first()
        if not mapping:
            mapping = BOMMapping(design_name=design_name)
            self.db.add(mapping)

        mapping.component_id = component_id
        mapping.mapping_type = mapping_type
        mapping.is_verified = is_verified

    def _ensure_mapping_exists(self, design_name: str):
        exists = self.db.query(BOMMapping).filter(BOMMapping.design_name == design_name).first()
        if not exists:
            new_mapping = BOMMapping(
                design_name=design_name,
                mapping_type="auto",
                is_verified=False
            )
            self.db.add(new_mapping)
