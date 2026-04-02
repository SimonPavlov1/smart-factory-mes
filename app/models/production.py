from sqlalchemy import Column, Integer, String, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("product_types.id"))
    target_qty = Column(Integer)
    status = Column(String, default="Draft")
    created_at = Column(DateTime, default=datetime.utcnow)

    items = relationship("Item", back_populates="order")

class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    serial_number = Column(String, unique=True, index=True)
    status = Column(String)

    order = relationship("Order", back_populates="items")
    stages = relationship("ItemStage", back_populates="item")

class ItemStage(Base):
    __tablename__ = "item_stages"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.id"))
    stage_name = Column(String)
    worker_name = Column(String)
    completed_at = Column(DateTime, default=datetime.utcnow)

    item = relationship("Item", back_populates="stages")