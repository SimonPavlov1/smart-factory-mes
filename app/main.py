from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import engine, Base
from app.api import inventory, production, procurement, manufacturing

# Создаем все таблицы в базе данных на основе наших моделей.
# Если база данных пуста или файла sql_app.db нет, он будет создан автоматически.
# В продакшене обычно используются миграции (Alembic), но для текущего этапа это идеальный вариант.
Base.metadata.create_all(bind=engine)

# Инициализируем FastAPI с метаданными для Swagger-документации.
app = FastAPI(
    title="Smart Factory MES API",
    description="Система управления составом изделий (BOM) и складским учетом комплектации.",
    version="1.1.0"
)

# Настройка CORS (Cross-Origin Resource Sharing).
# Необходима, чтобы твой фронтенд (например, на React/Vite) мог делать запросы к бэкенду.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5174"], # Адрес, на котором обычно запущен фронтенд
    allow_credentials=True,
    allow_methods=["*"],  # Разрешаем все типы запросов (GET, POST, DELETE и др.)
    allow_headers=["*"],  # Разрешаем любые HTTP-заголовки
)

# Подключаем модули API (роутеры) с логическим разделением.
# Теперь все эндпоинты будут сгруппированы в документации (/docs).
app.include_router(inventory.router, prefix="/inventory", tags=["Склад (Inventory)"])
app.include_router(production.router, prefix="/production", tags=["Производство (Production)"])
app.include_router(procurement.router, prefix="/procurement", tags=["Закупки (Procurement)"])
app.include_router(manufacturing.router)

@app.get("/", tags=["Системные"])
def read_root():
    """Проверка статуса API."""
    return {
        "status": "online",
        "service": "Smart Factory MES",
        "version": "1.1.0",
        "documentation": "/docs"
    }