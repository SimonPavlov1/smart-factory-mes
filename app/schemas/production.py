from pydantic import BaseModel, Field
from typing import List, Optional


class BOMItemBase(BaseModel):
    """
    Базовая структура строки состава (BOM).
    Эти поля — прямой слепок из таблицы ПЭ3 (Перечень элементов).
    """
    designators: str = Field(
        ...,
        example="C36-C47",
        description="Позиционные обозначения на плате"
    )
    design_name: str = Field(
        ...,
        example="Конденсатор CC0603MRX5R8BB106 YAGEO",
        description="Текст из колонки 'Наименование' (основа для поиска)"
    )
    quantity: float = Field(
        ...,
        example=12.0,
        description="Количество единиц на 1 изделие"
    )


class BOMItemCreate(BOMItemBase):
    """
    Схема для создания записи или ручного ввода.
    Включает технические поля, необходимые для сопоставления со складом.
    """
    # Если мы не знаем ID детали на складе, ставим 0
    resource_id: Optional[int] = Field(default=0, description="ID компонента или узла в базе")

    # 'component' (покупное) или 'product' (собственная сборка)
    resource_type: str = Field(default="component", description="Тип ресурса")

    # Флаг: удалось ли системе найти деталь в справочнике
    is_resolved: bool = Field(default=False, description="Статус сопоставления")

    # Группа ТМЦ (заполняется парсером или человеком)
    category: Optional[str] = Field(default=None, description="Категория (Резисторы, ИС и т.д.)")

    # Флаг вложенности: является ли эта строка другой платой/сборкой
    is_assembly: bool = Field(default=False, description="Является ли позиция узлом")

    # Список вложенных компонентов (для рекурсивной сборки изделий)
    components: List['BOMItemCreate'] = Field(default_factory=list)


# Необходимая команда для работы рекурсии (когда BOMItemCreate содержит List[BOMItemCreate])
BOMItemCreate.update_forward_refs()


class BOMItemResponse(BOMItemBase):
    """
    Схема для выдачи данных из БД.
    Используется для отображения состава в интерфейсе.
    """
    id: int
    resource_id: Optional[int]
    is_resolved: bool

    class Config:
        # Позволяет Pydantic читать данные напрямую из объектов SQLAlchemy
        from_attributes = True


class BOMUploadResponse(BaseModel):
    """Ответ сервера после массовой загрузки или парсинга документа"""
    product_id: int
    total_items: int
    items: List[BOMItemResponse]


class ProductCreateSchema(BaseModel):
    """
    Схема создания нового изделия (верхний уровень).
    Заполняется данными из 'штампа' чертежа или вручную.
    """
    name: str = Field(..., example="Плата управления", description="Название изделия")
    drawing_number: str = Field(..., example="РСДТ.421243.320", description="Децимальный номер")
    version: str = Field(default="1", description="Версия/ревизия КД")
    is_final: bool = Field(default=False, description="Флаг готового продукта (не полуфабрикат)")

    # Список всех строк состава, включая вложенные узлы
    components: List[BOMItemCreate] = Field(default_factory=list)