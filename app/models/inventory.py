from sqlalchemy import Column, Integer, String, Float, ForeignKey, Text
from app.database import Base


class Component(Base):
    """
    Справочник покупных комплектующих (ТМЦ).
    Содержит неизменяемые технические характеристики деталей.
    """
    __tablename__ = "components"

    id = Column(Integer, primary_key=True, index=True, comment="Уникальный ID детали")

    # Основная информация
    name = Column(String, nullable=False, index=True, comment="Наименование (напр. Резистор)")
    part_number = Column(String, unique=True, index=True, nullable=False, comment="Артикул производителя")

    # Классификация
    category = Column(String, index=True, comment="Группа (IC, Resistors, Connectors)")
    type = Column(String, comment="Подтип (напр. MLCC, Тонкопленочный)")

    # Технические параметры (Атрибутивный учет)
    package = Column(String, index=True, comment="Тип корпуса (0603, SOT-23)")
    value = Column(String, comment="Номинал (10k, 100nF, 3.3V)")
    tolerance = Column(String, comment="Допуск/Точность (1%, 5%, X7R)")

    # Логистика
    unit = Column(String, default="pcs", comment="Единица измерения (шт, м, кг)")
    description = Column(Text, nullable=True, comment="Расширенное текстовое описание")
    datasheet_path = Column(String, nullable=True, comment="Путь к PDF-файлу документации")


class Stock(Base):
    """
    Складской учет и адресное хранение.
    Хранит информацию о физическом количестве и резервах.
    """
    __tablename__ = "stock"

    id = Column(Integer, primary_key=True, index=True, comment="ID записи остатка")

    # Связи
    component_id = Column(
        Integer,
        ForeignKey("components.id"),
        unique=True,
        nullable=False,
        comment="Ссылка на ID компонента"
    )

    # Количественные показатели
    actual_qty = Column(Float, default=0.0, comment="Фактическое количество на складе")
    reserved_qty = Column(Float, default=0.0, comment="Количество в мягком резерве")

    # Адресное хранение
    location = Column(String, index=True, comment="Адрес ячейки хранения (напр. A-01-05)")