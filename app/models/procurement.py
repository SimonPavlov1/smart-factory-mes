from sqlalchemy import Column, Integer, String, Float, ForeignKey, Date
from sqlalchemy.orm import relationship
from app.database import Base


class PurchaseOrder(Base):
    """Заказ поставщику"""
    __tablename__ = "purchase_orders"
    id = Column(Integer, primary_key=True, index=True)
    supplier_name = Column(String, nullable=False)
    status = Column(String, default="Draft")  # Draft, Received

    # Добавлен cascade
    items = relationship("PurchaseItem", back_populates="order", cascade="all, delete-orphan")


class PurchaseItem(Base):
    """Позиция в заказе"""
    __tablename__ = "purchase_items"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("purchase_orders.id"))
    component_id = Column(Integer, ForeignKey("components.id"))
    qty = Column(Float, nullable=False)
    price = Column(Float, nullable=True)

    order = relationship("PurchaseOrder", back_populates="items")