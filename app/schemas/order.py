from pydantic import BaseModel, Field
from datetime import datetime
from typing import Any, Dict, List, Optional

# --- Схемы для ОТОБРАЖЕНИЯ (Out) ---

class ProductMinOut(BaseModel):
    """Минимальная информация об изделии для отображения в составе заказа"""
    id: int
    name: str
    sku: Optional[str] = None

    class Config:
        from_attributes = True


class OrderItemOut(BaseModel):
    """Позиция заказа при выдаче на фронтенд"""
    id: int
    product_id: int
    quantity: int
    product: ProductMinOut  # Наша магия: вкладываем сюда объект с именем и артикулом

    class Config:
        from_attributes = True


class OrderOut(BaseModel):
    """Схема самого заказа со всем списком его позиций"""
    id: int
    customer_name: str
    status: str
    legacy_status: Optional[str] = None
    progress: Optional[Dict[str, Any]] = None
    created_at: datetime
    planned_delivery_date: Optional[datetime] = None
    items: List[OrderItemOut]  # Список позиций

    class Config:
        from_attributes = True


# --- Схемы для СОЗДАНИЯ (Create) ---

class OrderItemCreate(BaseModel):
    """Схема для добавления одной позиции при создании заказа"""
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0)


class OrderCreate(BaseModel):
    """Схема, которую присылает фронтенд при создании нового заказа"""
    customer_name: str = Field(min_length=1)
    planned_delivery_date: Optional[str] = None
    items: List[OrderItemCreate]
