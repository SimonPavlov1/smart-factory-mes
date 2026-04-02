from app.database import engine, Base
# Импортируем все модели, чтобы Base знала о них
from app.models.product import ProductType, ProductBOM
from app.models.inventory import Component, Stock, Reservation
from app.models.production import Order, Item, ItemStage

def init_db():
    print("Создание таблиц в базе данных...")
    Base.metadata.create_all(bind=engine)
    print("База данных готова!")

if __name__ == "__main__":
    init_db()