from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.inventory import Component

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