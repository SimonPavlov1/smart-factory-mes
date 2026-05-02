from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class PurchaseItemCreate(BaseModel):
    component_id: int
    qty: float = Field(gt=0)
    price: float | None = Field(default=None, ge=0)


class PurchaseOrderCreate(BaseModel):
    supplier_name: str = Field(min_length=1)
    items: list[PurchaseItemCreate] = Field(min_length=1)


class PurchaseDeliveryUpdate(BaseModel):
    tracking_code: str | None = None
    arrival_date: date | None = None
    invoice_ref: str | None = None
    status: str | None = None


class PurchaseReceiveRequest(BaseModel):
    location: str = "Warehouse-1"


class PurchaseItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    component_id: int
    qty: float
    price: float | None = None


class PurchaseOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    supplier_name: str
    status: str
    invoice_ref: str | None = None
    tracking_code: str | None = None
    arrival_date: date | None = None


class PurchaseOrderDetail(PurchaseOrderRead):
    items: list[PurchaseItemRead]
