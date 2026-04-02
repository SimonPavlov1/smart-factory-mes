from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.inventory import Component
from app.models.production import Order
from app.schemas.order import OrderCreate

app = FastAPI()

# Подключение к базе
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/")
def read_root():
    return {"message": "Welcome to Smart Factory API"}

@app.get("/inventory")
def get_inventory(db: Session = Depends(get_db)):
    # Получаем все компоненты из базы
    components = db.query(Component).all()
    return components

@app.post("/inventory")
def create_order(payload: OrderCreate, db: Session = Depends(get_db)):
    # Создаем запись в таблице заказов
    new_order = Order(
        product_id=payload.product_id,
        target_qty=payload.target_qty,
        status="Новый"
    )
    db.add(new_order)
    db.commit()
    db.refresh(new_order)
    return {"status": "Заказ принят", "order_id": new_order.id}