from app.database import engine, Base
from app.models.inventory import Component, Stock
from app.models.procurement import PurchaseOrder, PurchaseItem
from app.models.production import ProductType, ProductBOM, Order, Reservation, Item


def init_db():
    print("Удаляю старую базу (если есть)...")
    Base.metadata.drop_all(bind=engine)

    print("Создаю новые таблицы...")
    Base.metadata.create_all(bind=engine)
    print("Готово! Проверь файл factory.db")


if __name__ == "__main__":
    init_db()
