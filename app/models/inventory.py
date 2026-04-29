from sqlalchemy import Column, Integer, String, Float, ForeignKey, Text, JSON
from sqlalchemy.orm import relationship
from app.database import Base


class Component(Base):
    """
    Универсальный справочник товарно-материальных ценностей (ТМЦ).

    Модель поддерживает гибкую схему данных: базовые параметры (артикул, корпус)
    вынесены в отдельные колонки для быстрого поиска, а специфические характеристики
    (параметры микросхем, допуски резисторов) хранятся в JSON-поле specifications.
    """
    __tablename__ = "components"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True, comment="Наименование типа (напр. 'Конденсатор')")
    part_number = Column(String, unique=True, index=True, nullable=False, comment="Manufacturer Part Number (MPN)")
    category = Column(String, index=True, comment="Категория (Резисторы, Микросхемы и т.д.)")
    package = Column(String, index=True, comment="Тип корпуса (0603, SOT-23, LQFP-64)")

    # Общие электротехнические параметры для быстрой фильтрации
    value = Column(String, comment="Номинальное значение (напр. '10uF', '4.7k')")
    voltage = Column(Float, nullable=True, comment="Рабочее напряжение, В")

    # Поле для гибких метаданных.
    # Позволяет хранить уникальные свойства разных типов деталей без изменения схемы БД.
    # Для микросхем: интерфейсы, ток, количество ядер.
    # Для транзисторов: hFE, ток стока и т.д.
    specifications = Column(JSON, nullable=True, comment="Дополнительные характеристики в формате JSON")

    # Связь с таблицей фактического наличия на складе
    stock = relationship("Stock", back_populates="component", uselist=False)


class Stock(Base):
    """
    Данные о физическом наличии и расположении ТМЦ на складе.
    Связывает справочную карточку Component с конкретным местом хранения и количеством.
    """
    __tablename__ = "stock"
    id = Column(Integer, primary_key=True, index=True)

    # Ссылка на карточку компонента (может быть пустой, если на остатках готовое изделие)
    component_id = Column(Integer, ForeignKey("components.id"), unique=True, nullable=True)

    # Ссылка на тип готового изделия (если на складе лежит собранная плата/блок)
    product_id = Column(Integer, ForeignKey("product_types.id"), unique=True, nullable=True)

    actual_qty = Column(Float, default=0.0, comment="Текущее количество на складе")
    location = Column(String, default="Warehouse-1", comment="Адрес хранения (стеллаж, ячейка)")

    component = relationship("Component", back_populates="stock")