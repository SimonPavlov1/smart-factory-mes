from pydantic import BaseModel
from typing import List, Optional

class BOMItemBase(BaseModel):
    """Базовая схема для элемента состава изделия (BOM)"""
    design_name: str           # Оригинальное название из ПЭ3
    designators: Optional[str] # Позиционные обозначения (C1, R1-R5)
    quantity: float            # Количество
    category: Optional[str]    # Категория, определенная парсером

class BOMItemCreate(BOMItemBase):
    """Схема для создания/импорта элемента"""
    resource_id: int = 0       # ID из справочника (0 если не найдено)
    is_resolved: bool = False  # Флаг: найдена ли деталь в базе
    is_assembly: bool = False  # Флаг: является ли это узлом/сборкой
    components: Optional[List['BOMItemCreate']] = [] # Для вложенных сборок

# Обновляем ссылки для поддержки рекурсии (вложенных компонентов)
BOMItemCreate.update_forward_refs()

class ProductCreateSchema(BaseModel):
    """Схема для создания всего изделия целиком"""
    name: str
    drawing_number: str
    version: str
    is_final: bool = False
    components: List[BOMItemCreate]

class BOMUploadResponse(BaseModel):
    """Схема ответа после загрузки и парсинга файла"""
    product_id: int
    total_items: int
    items: List[BOMItemCreate]