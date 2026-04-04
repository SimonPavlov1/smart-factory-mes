from app.database import engine, Base

def init_db():
    print("Создаю таблицы в базе данных...")
    Base.metadata.create_all(bind=engine)
    print("База данных успешно инициализирована!")

if __name__ == "__main__":
    init_db()