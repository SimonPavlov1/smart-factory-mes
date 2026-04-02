from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Float
from app.database import Base

class ProductType(Base):
    __tablename__ = "product_types"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    decimal_number = Column(String, unique=True)
    is_subassembly = Column(Boolean, default=False)

class ProductBOM(Base):
    __tablename__ = "product_bom"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("product_types.id"))
    component_id = Column(Integer, ForeignKey("components.id"))
    quantity = Column(Float)
    designator = Column(String) # Например, R1, C5