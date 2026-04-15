from sqlalchemy import Column, Integer, String, Float, ForeignKey, Date
from sqlalchemy.orm import relationship
from app.database import Base


class PurchaseOrder(Base):
    """
    Транзакции закупок (Заказы поставщикам / Счета).
    Управляет процессом пополнения склада от внешних поставщиков.
    """
    __tablename__ = "purchase_orders"

    id = Column(Integer, primary_key=True, index=True, comment="ID транзакции закупки")

    # Информация о поставке
    supplier_name = Column(String, nullable=False, comment="Название поставщика (напр. Чип и Дип)")
    status = Column(
        String,
        index=True,
        default="Draft",
        comment="Статус: Draft, Ordered, In Transit, Received"
    )

    # Документация и логистика
    invoice_ref = Column(String, nullable=True, comment="Ссылка на файл счета или номер договора")
    tracking_code = Column(String, nullable=True, comment="Трек-номер транспортной компании")
    arrival_date = Column(Date, nullable=True, comment="Плановая дата прибытия деталей")

    # Связи (позволяет обращаться к позициям через order.items)
    items = relationship("PurchaseItem", back_populates="order", cascade="all, delete-orphan")


class PurchaseItem(Base):
    """
    Спецификация закупки (строки счета).
    Связывает конкретные компоненты с заказом поставщику.
    """
    __tablename__ = "purchase_items"

    id = Column(Integer, primary_key=True, index=True, comment="ID строки в заказе")

    # Внешние ключи
    order_id = Column(
        Integer,
        ForeignKey("purchase_orders.id"),
        nullable=False,
        comment="ID родительского заказа"
    )
    component_id = Column(
        Integer,
        ForeignKey("components.id"),
        nullable=False,
        comment="ID закупаемого компонента"
    )

    # Экономические показатели
    qty = Column(Float, nullable=False, comment="Количество в заказе")
    price = Column(Float, nullable=True, comment="Цена за единицу (для финансового учета)")

    # Обратные связи
    order = relationship("PurchaseOrder", back_populates="items")
