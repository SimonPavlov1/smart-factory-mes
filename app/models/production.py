from sqlalchemy import Column, Integer, String, Boolean, Text, Float, ForeignKey, func, and_
from sqlalchemy.orm import relationship
from sqlalchemy.sql.sqltypes import DateTime
from app.database import Base


class BOMMapping(Base):
    """
    Глобальная база знаний сопоставлений.
    Хранит исторические данные: какой текст из ПЭ3 соответствует какому ID в базе.
    Позволяет системе автоматически подставлять детали при повторном вводе тех же позиций.
    """
    __tablename__ = "bom_mappings"

    id = Column(Integer, primary_key=True)
    design_name = Column(String, unique=True, index=True, nullable=False,
                         comment="Строка-ключ (напр. 'Конденсатор ССО603...'). Основа для поиска.")

    component_id = Column(Integer, ForeignKey("components.id"), nullable=True,
                          comment="Ссылка на складскую позицию. NULL, если сопоставление не выполнено.")

    mapping_type = Column(String, default="auto",
                          comment="Тип привязки: 'auto' (робот) или 'manual' (человек).")

    is_verified = Column(Boolean, default=False,
                         comment="Подтверждено ли сопоставление ответственным лицом.")

    component = relationship("Component")


class ProductType(Base):
    """
    Реестр выпускаемой продукции и узлов.
    Здесь хранятся 'карточки' изделий, которые компания умеет собирать (Платы, Блоки, Приборы).
    """
    __tablename__ = "product_types"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, comment="Понятное имя (напр. 'Плата управления двигателем')")
    sku = Column(String, unique=True, index=True, comment="Внутренний код/артикул готового товара")
    drawing_number = Column(String, index=True, nullable=True, comment="Децимальный номер по ГОСТ (напр. РСДТ...)")

    is_subassembly = Column(Boolean, default=False,
                            comment="True, если это не готовый продукт, а вложенный узел (полуфабрикат).")

    revision = Column(String, default="1.0", comment="Версия КД или ревизия печатной платы.")
    bill_of_materials_url = Column(String, nullable=True, comment="Путь к чертежам, PDF или исходникам проекта.")
    description = Column(Text, nullable=True, comment="Технические особенности или нюансы сборки.")

    # Связи
    components = relationship("ProductBOM", back_populates="product", cascade="all, delete-orphan")
    orders = relationship("Order", back_populates="product_type")


class ProductBOM(Base):
    """
    Спецификация состава изделия (BOM).
    Описывает 'рецепт' сборки: какие детали и в каком количестве нужны на 1 единицу.
    """
    __tablename__ = "product_boms"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("product_types.id"), comment="Владелец спецификации")

    design_name = Column(String, nullable=False, index=True,
                         comment="Текст из ПЭ3. Позволяет хранить состав даже без привязки к складу.")

    designators = Column(String, nullable=True,
                         comment="Адреса на плате (напр. R1, R5-R10). Важно для монтажников.")

    resource_id = Column(Integer, nullable=True, index=True,
                         comment="ID ресурса. Ссылается на Component или ProductType (зависит от типа).")

    resource_type = Column(String, nullable=False, default="component",
                           comment="Дискриминатор: 'component' (покупное) или 'product' (свой узел).")

    quantity = Column(Float, nullable=False, default=1.0, comment="Количество на 1 шт. готового изделия.")

    is_resolved = Column(Boolean, default=False,
                         comment="Маркер готовности: найдена ли деталь в складской базе.")

    product = relationship("ProductType", back_populates="components", foreign_keys=[product_id])

    # Полиморфные связи для доступа к данным ресурса
    resource_component = relationship(
        "Component",
        primaryjoin="and_(ProductBOM.resource_id==Component.id, ProductBOM.resource_type=='component')",
        foreign_keys=[resource_id],
        viewonly=True,
        overlaps="resource_product"
    )
    resource_product = relationship(
        "ProductType",
        primaryjoin="and_(ProductBOM.resource_id==ProductType.id, ProductBOM.resource_type=='product')",
        foreign_keys=[resource_id],
        viewonly=True,
        overlaps="resource_component"
    )


class Order(Base):
    """
    Заказ на производство (Производственное задание).
    Фиксирует намерение собрать X единиц определенного изделия.
    """
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("product_types.id"), nullable=False)
    target_qty = Column(Integer, nullable=False, comment="План выпуска (штук).")
    status = Column(String, default="New", index=True, comment="Этап: New -> In Progress -> Completed.")
    created_at = Column(DateTime, server_default=func.now(), comment="Время постановки в очередь.")

    product_type = relationship("ProductType", back_populates="orders")
    reservations = relationship("Reservation", back_populates="order")
    items = relationship("Item", back_populates="order")


class Reservation(Base):
    """
    Система бронирования остатков.
    Виртуально 'замораживает' детали на складе под конкретный производственный заказ.
    """
    __tablename__ = "reservations"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), index=True, nullable=False)
    component_id = Column(Integer, ForeignKey("components.id"), index=True, nullable=False)
    qty = Column(Float, nullable=False, comment="Количество забронированных единиц.")

    order = relationship("Order", back_populates="reservations")


class Item(Base):
    """
    Учет готовой продукции по серийным номерам.
    Запись о физически собранном экземпляре изделия.
    """
    __tablename__ = "items"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), index=True, nullable=False)
    serial_number = Column(String, unique=True, index=True, nullable=False, comment="SN изделия.")
    test_result = Column(String, nullable=True, comment="Результат прохождения ОТК.")

    order = relationship("Order", back_populates="items")