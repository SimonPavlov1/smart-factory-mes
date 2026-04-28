from sqlalchemy import Column, Integer, String, Float, ForeignKey, Text
from sqlalchemy.orm import relationship
from app.database import Base


class Component(Base):
    """Справочник ТМЦ. Сюда парсер будет подставлять извлеченные данные."""
    __tablename__ = "components"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True, comment="Напр. 'Резистор'")
    part_number = Column(String, unique=True, index=True, nullable=False, comment="MPN артикул")
    category = Column(String, index=True)
    package = Column(String, index=True, comment="Корпус, напр. '0603'")

    # Эти поля важны для сопоставления
    value = Column(String, comment="Номинал строкой")
    voltage = Column(Float, nullable=True)

    stock = relationship("Stock", back_populates="component", uselist=False)


class Stock(Base):
    """Остатки на складе."""
    __tablename__ = "stock"
    id = Column(Integer, primary_key=True, index=True)
    component_id = Column(Integer, ForeignKey("components.id"), unique=True, nullable=True)
    product_id = Column(Integer, ForeignKey("product_types.id"), unique=True, nullable=True)
    actual_qty = Column(Float, default=0.0)
    location = Column(String, default="Warehouse-1")

    component = relationship("Component", back_populates="stock")