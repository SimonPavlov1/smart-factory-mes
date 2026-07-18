from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text

from app.database import engine, Base, SessionLocal
from app.api import auth, inventory, production, procurement, manufacturing, tasks
from app.services.auth_service import ensure_default_admin

# Создаем все таблицы в базе данных на основе наших моделей.
# Если база данных пуста или файла sql_app.db нет, он будет создан автоматически.
# В продакшене обычно используются миграции (Alembic), но для текущего этапа это идеальный вариант.
Base.metadata.create_all(bind=engine)
with engine.begin() as conn:
    columns = {column["name"] for column in inspect(conn).get_columns("workflow_tasks")}
    if "assigned_user_id" not in columns:
        conn.execute(text("ALTER TABLE workflow_tasks ADD COLUMN assigned_user_id INTEGER"))
    if "started_at" not in columns:
        conn.execute(text("ALTER TABLE workflow_tasks ADD COLUMN started_at DATETIME"))
    bom_columns = {column["name"] for column in inspect(conn).get_columns("product_boms")}
    if "parent_id" not in bom_columns:
        conn.execute(text("ALTER TABLE product_boms ADD COLUMN parent_id INTEGER"))
    if "item_type" not in bom_columns:
        conn.execute(text("ALTER TABLE product_boms ADD COLUMN item_type VARCHAR DEFAULT 'component' NOT NULL"))
    if "operation_role" not in bom_columns:
        conn.execute(text("ALTER TABLE product_boms ADD COLUMN operation_role VARCHAR"))
    if "sort_order" not in bom_columns:
        conn.execute(text("ALTER TABLE product_boms ADD COLUMN sort_order INTEGER DEFAULT 0 NOT NULL"))
    product_columns = {column["name"] for column in inspect(conn).get_columns("product_types")}
    if "photo_url" not in product_columns:
        conn.execute(text("ALTER TABLE product_types ADD COLUMN photo_url VARCHAR"))
    if "attachments" not in product_columns:
        conn.execute(text("ALTER TABLE product_types ADD COLUMN attachments JSON"))
with SessionLocal() as db:
    ensure_default_admin(db)

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
app.include_router(tasks.router)
app.include_router(auth.router)

@app.get("/", tags=["Системные"])
def read_root():
    """Проверка статуса API."""
    return {
        "status": "online",
        "service": "Smart Factory MES",
        "version": "1.1.0",
        "documentation": "/docs"
    }
