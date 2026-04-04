from sqlalchemy import Column, Integer, String, Boolean, Text, Float, ForeignKey, func
from sqlalchemy.sql.sqltypes import DateTime

from app.database import Base

class ProductType(Base):
    """
    Реестр любых производимых изделий (конечные приборы или узлы).
    Описывает ЧТО мы производим и по какой документации.
    """
    __tablename__ = "product_types"

    id = Column(Integer, primary_key=True, index=True, comment="Уникальный ID изделия")
    name = Column(String, nullable=False, comment="Коммерческое название прибора")
    sku = Column(String, unique=True, index=True, comment="Сокращенное название")
    drawing_number = Column(String, index=True, nullable=True, comment="Децимальный номер по ГОСТ/КД")
    is_subassembly = Column(Boolean, default=False, comment="Признак узла (не самостоятельного устройства)")
    revision = Column(String, default="1.0", comment="Версия конструкторской документации")
    bill_of_materials_url = Column(String, nullable=True, comment="Ссылка на документацию")
    description = Column(Text, nullable=True, comment="Описание функционала и ТУ")

class ProductBOM(Base):
    """
    Спецификация (BOM). Описывает ИЗ ЧЕГО состоит изделие.
    Связывает ProductType с компонентами или другими узлами.
    """
    __tablename__ = "product_boms"

    id = Column(Integer, primary_key=True, comment="ID строки спецификации")
    product_id = Column(Integer, ForeignKey("product_types.id"), comment="ID родительского изделия")
    resource_id = Column(Integer, nullable=False, comment="ID того, что берем (деталь или узел)")
    resource_type = Column(String, nullable=False, comment="Маркер 'component  или 'subassembly'")
    quantity = Column(Float, nullable=False, comment="Кол-во на 1 ед. (шт, метры)")
    designators = Column(String, nullable=True, comment="Поз. обозначения на плате (R1, C5)")

class Order(Base):
    """
    Производственные заказы. Задание на сборку партии изделий.
    """
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True, comment="Номер заказа")
    product_id = Column(Integer, ForeignKey("product_types.id"), nullable=False, comment="Что собираем")
    target_qty = Column(Integer, nullable=False, comment="План выпуска (кол-во шт)")
    status = Column(String, default="New", index=True, comment="Статус (New, In Progress, Done, Canceled)")
    created_at = Column(DateTime, server_default=func.now(), comment="Дата и время создания")

class Reservation(Base):
    """
    Резервы. Блокирует детали на складе под конкретный производственный заказ.
    """
    __tablename__ = "reservations"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), index=True, nullable=False)
    component_id = Column(Integer, ForeignKey("components.id"), index=True, nullable=False)
    qty = Column(Float, nullable=False, comment="Сколько единиц заблокировано")

class Item(Base):
    """
    Готовые изделия (экземпляры). Поштучный учет с серийными номерами.
    """
    __tablename__ = "items"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), index=True, nullable=False, comment="Из какого заказа прибор")
    serial_number = Column(String, unique=True, index=True, nullable=False, comment="Уникальный S/N прибора")
    test_result = Column(String, nullable=True, comment="Результат финальной проверки (Pass/Fail)")