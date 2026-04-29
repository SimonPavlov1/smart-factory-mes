from pydantic import BaseModel, Field
from typing import Optional, Dict, Any


class ComponentCreate(BaseModel):
    """Схема для создания нового компонента в справочнике."""
    name: str = Field(..., example="Резистор")
    part_number: str = Field(..., example="RC0603FR-074K99L")
    category: Optional[str] = Field(None, example="Резисторы")
    package: Optional[str] = Field(None, example="0603")
    value: Optional[str] = Field(None, example="4.99k")
    value_numeric: Optional[float] = Field(None, example="10000.0")
    voltage: Optional[float] = Field(None, example=50.0)

    # Гибкое поле для доп. характеристик
    specifications: Optional[Dict[str, Any]] = Field(
        None,
        example={"tolerance": "1%", "manufacturer": "Yageo"}
    )

    class Config:
        from_attributes = True


class ComponentResponse(ComponentCreate):
    """Схема для ответа сервера (с ID)."""
    id: int

    class Config:
        from_attributes = True