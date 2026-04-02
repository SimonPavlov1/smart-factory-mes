from pydantic import BaseModel

class OrderCreate(BaseModel):
    product_id: int    # ID изделия, которое хотим собрать (например, 1)
    target_qty: int    # Сколько штук хотим сделать (например, 10)