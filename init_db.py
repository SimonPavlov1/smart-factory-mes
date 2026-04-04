from app.database import engine, Base
from app.models.inventory import Component, Stock
# Импортируй сюда все остальные модели, которые будешь создавать, например:
# from app.models.production import ProductType, ProductBOM, Order

def init_db():
    print("Создаю таблицы в базе данных...")
    # Эта команда берет все классы, наследующие Base,
    # и создает для них таблицы в файле, указанном в config.py
    Base.metadata.create_all(bind=engine)
    print("База данных успешно инициализирована!")

if __name__ == "__main__":
    init_db()