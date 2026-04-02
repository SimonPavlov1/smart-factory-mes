from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime

class Component(Base):
    __tablename__ = "components"

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String)  # Резисторы, Конденсаторы
    value = Column(String)     # 10k, 1uF
    package = Column(String)   # 0805, SOT-23
    part_number = Column(String, unique=True, index=True)
    datasheet_path = Column(String, nullable=True)

    stock = relationship("Stock", uselist=False, back_populates="component")

class Stock(Base):
    __tablename__ = "stock"

    component_id = Column(Integer, ForeignKey("components.id"), primary_key=True)
    actual_qty = Column(Float, default=0.0)
    reserved_qty = Column(Float, default=0.0)
    location = Column(String)

    component = relationship("Component", back_populates="stock")

class Reservation(Base):
    __tablename__ = "reservations"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    component_id = Column(Integer, ForeignKey("components.id"))
    qty_reserved = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)