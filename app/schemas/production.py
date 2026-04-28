from pydantic import BaseModel, Field
from typing import List, Optional


class BOMItemBase(BaseModel):
    """Базовая схема элемента из ПЭ3 (соответствует колонкам документа)"""
    # Колонка "Поз. обозначение" [cite: 115, 116]
    designators: str = Field(..., example="C36-C47", description="Позиционные обозначения")

    # Колонка "Наименование"
    design_name: str = Field(..., example="Конденсатор CC0603MRX5R8BB106 YAGEO",
                             description="Полный текст из документа")

    # Колонка "Кол."
    quantity: float = Field(..., example=12.0, description="Количество элементов")


class BOMItemCreate(BOMItemBase):
    """Схема для создания и процесса сопоставления"""
    # Ссылка на id из Component (inventory.py)
    resource_id: int = 0

    # Тип ресурса: покупная деталь (component) или собственный узел (product)
    resource_type: str = "component"

    # Статус: нашла ли система деталь на складе автоматически
    is_resolved: bool = False

    # Категория для парсера (Конденсаторы, Резисторы и т.д.) [cite: 118, 196, 199]
    category: Optional[str] = None

    # Для многоуровневых спецификаций (сборка внутри сборки)
    is_assembly: bool = False
    components: Optional[List['BOMItemCreate']] = []


# Обновляем ссылки для поддержки вложенности
BOMItemCreate.update_forward_refs()


class BOMItemResponse(BOMItemBase):
    """Схема для отдачи данных на фронтенд (чтение из БД)"""
    id: int
    resource_id: int
    is_resolved: bool

    class Config:
        from_attributes = True


class BOMUploadResponse(BaseModel):
    """Схема ответа после массовой обработки списка строк"""
    product_id: int
    total_items: int
    items: List[BOMItemResponse]


class ProductCreateSchema(BaseModel):
    """Схема для инициализации нового изделия (согласно штампу ПЭ3)"""
    name: str = Field(..., example="Плата управления")  # [cite: 179]
    drawing_number: str = Field(..., example="РСДТ.421243.320")  # [cite: 178]
    version: Optional[str] = "1"
    is_final: bool = False
    components: List[BOMItemCreate]