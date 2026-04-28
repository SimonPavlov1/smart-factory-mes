from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Адрес базы данных. В данном случае используем локальный файл SQLite.
# 'sqlite:///./sql_app.db' создаст файл базы прямо в корневой папке проекта.
SQLALCHEMY_DATABASE_URL = "sqlite:///./sql_app.db"

# Создаем "движок" (Engine) — это точка входа для общения с БД.
# connect_args={"check_same_thread": False} необходим только для SQLite,
# чтобы несколько потоков FastAPI могли одновременно обращаться к базе.
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

# Настраиваем фабрику сессий (SessionLocal).
# autocommit=False: транзакции будут завершаться только командой db.commit().
# autoflush=False: данные не будут отправляться в базу до явного запроса.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Базовый класс для всех наших моделей (Component, ProductBOM и т.д.).
# Благодаря ему SQLAlchemy понимает, какие классы нужно превращать в таблицы.
Base = declarative_base()


def get_db():
    """
    Генератор сессии базы данных (Dependency Injection).

    Используется в API роутах. Создает новую сессию для каждого запроса
    и гарантированно закрывает её после завершения обработки (даже в случае ошибки).
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()