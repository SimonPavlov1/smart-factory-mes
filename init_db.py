from app.database import engine, Base
# Импортируем все модели, чтобы SQLAlchemy "увидела" их перед созданием таблиц
from app.models.inventory import Component, Stock
from app.models.production import ProductType, ProductBOM, Order, Reservation, Item, BOMMapping
from app.models.procurement import PurchaseOrder, PurchaseItem


def init_db():
    """
    Скрипт инициализации базы данных.
    ВНИМАНИЕ: drop_all удалит все текущие данные в файле .db!
    """
    print("--- Запуск инициализации БД ---")

    # Шаг 1: Удаление старых таблиц
    # Это полезно на этапе разработки, когда ты часто меняешь состав полей в models.py
    print("Удаление старых таблиц (очистка данных)...")
    Base.metadata.drop_all(bind=engine)

    # Шаг 2: Создание новых таблиц на основе актуальных моделей
    print("Создание новых таблиц согласно актуальным моделям...")
    Base.metadata.create_all(bind=engine)

    print("--- Инициализация успешно завершена! ---")
    print("Файл базы данных готов к работе.")


if __name__ == "__main__":
    init_db()