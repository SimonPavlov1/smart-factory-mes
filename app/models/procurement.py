from sqlalchemy import Column, Date, Float, ForeignKey, Integer, String

from app.database import Base


class PurchaseOrder(Base):
    """
    Supplier purchase order header.
    Stores delivery, invoice, and workflow status data.
    """

    __tablename__ = "purchase_orders"

    id = Column(Integer, primary_key=True, index=True)
    supplier_name = Column(String, nullable=False, index=True)
    status = Column(String, default="Draft", index=True, nullable=False)
    invoice_ref = Column(String, nullable=True)
    tracking_code = Column(String, nullable=True)
    arrival_date = Column(Date, nullable=True)


class PurchaseItem(Base):
    """
    Purchase order line item.
    Points to a component expected from the supplier.
    """

    __tablename__ = "purchase_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("purchase_orders.id"), index=True, nullable=False)
    component_id = Column(Integer, ForeignKey("components.id"), index=True, nullable=False)
    qty = Column(Float, nullable=False)
    price = Column(Float, nullable=True)
