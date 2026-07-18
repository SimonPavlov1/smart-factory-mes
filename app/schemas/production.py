from pydantic import BaseModel, Field
from typing import List, Optional


class BOMItemBase(BaseModel):
    """
    Базовая структура элемента спецификации (BOM).
    Отражает основные колонки из конструкторского перечня элементов (ПЭ3).
    """
    designators: str = Field(
        ...,
        example="C36-C47",
        description="Позиционные обозначения на печатной плате"
    )
    design_name: str = Field(
        ...,
        example="Конденсатор CC0603MRX5R8BB106 YAGEO",
        description="Наименование детали (полный текст из документации)"
    )
    quantity: float = Field(
        ...,
        example=12.0,
        description="Количество единиц данного компонента на 1 изделие"
    )


class BOMItemCreate(BOMItemBase):
    """
    Схема для создания записи в составе изделия.
    Используется при импорте файлов или ручном вводе состава.
    """
    # Ссылка на складской ID (если система уже узнала деталь)
    resource_id: Optional[int] = Field(default=0, description="ID из базы склада или другой сборки")

    # 'component' (покупная деталь) или 'product' (узел собственного изготовления)
    resource_type: str = Field(default="component", description="Тип ресурса для логики резервирования")
    item_type: str = Field(default="component", description="Тип строки: assembly, component или operation")
    parent_id: Optional[int] = Field(default=None, description="Родительская строка состава")
    operation_role: Optional[str] = Field(default=None, description="Роль/участок для операции")
    sort_order: int = Field(default=0, description="Порядок строки внутри родителя")

    # Статус успешности автоматического сопоставления со складом
    is_resolved: bool = Field(default=False, description="Привязана ли строка к реальному товару")

    # Группа ТМЦ, определенная парсером (Резисторы, Конденсаторы и т.д.)
    category: Optional[str] = Field(default=None, description="Категория компонента")

    # Флаг многоуровневой структуры
    is_assembly: bool = Field(default=False, description="Является ли позиция вложенным узлом")

    # Список вложенных деталей (для реализации принципа 'матрешки')
    components: List['BOMItemCreate'] = Field(default_factory=list)


# Позволяет Pydantic обрабатывать рекурсивную вложенность (List['BOMItemCreate'])
BOMItemCreate.update_forward_refs()


class BOMItemResponse(BOMItemBase):
    """
    Схема для передачи данных на фронтенд.
    Добавляет системные ID, необходимые для работы интерфейса.
    """
    id: int
    resource_id: Optional[int]
    is_resolved: bool

    class Config:
        # Включает режим совместимости с объектами SQLAlchemy (ORM)
        from_attributes = True


class BOMUploadResponse(BaseModel):
    """Результат массовой обработки строк состава (например, после загрузки файла)"""
    product_id: int
    total_items: int
    items: List[BOMItemResponse]


class ProductCreateSchema(BaseModel):
    """
    Главная схема для регистрации нового изделия в системе.
    Объединяет общие данные об изделии и его полный состав.
    """
    name: str = Field(..., example="Плата управления", description="Понятное название изделия")
    drawing_number: str = Field(..., example="РСДТ.421243.320", description="Децимальный номер чертежа")
    version: str = Field(default="1", description="Версия КД или ревизия платы")
    is_final: bool = Field(default=False, description="True, если это готовый продукт для продажи")

    # Рекурсивный список всех компонентов и узлов
    components: List[BOMItemCreate] = Field(default_factory=list)
