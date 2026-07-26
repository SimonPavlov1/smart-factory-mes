# Smart Factory MES API

Backend CRM/MES «Проекты»: производственные заявки, BOM, склад, задачи, заводские номера, тестирование, ремонт, упаковка и готовая продукция.

## Быстрый запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

После запуска:

- API: `http://127.0.0.1:8000`;
- Swagger: `http://127.0.0.1:8000/docs`;
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`.

При пустой базе создаётся локальный администратор `admin / admin123`. Для production задайте `AUTH_SECRET_KEY`, `DEFAULT_ADMIN_USERNAME` и `DEFAULT_ADMIN_PASSWORD`.

## Проверка

```bash
.venv/bin/python -m compileall -q app tests
.venv/bin/python -m unittest discover -s tests -v
```

## Структура

```text
app/api/          HTTP API и проверка ролей
app/models/       SQLAlchemy-модели
app/schemas/      Pydantic-схемы
app/services/     производственный workflow и бизнес-правила
alembic/          миграции схемы
tests/            интеграционные производственные сценарии
docs/             технические спецификации
```

## Основные API-группы

| Префикс | Назначение |
|---|---|
| `/auth` | Авторизация |
| `/admin/users`, `/users` | Сотрудники и роли |
| `/inventory` | Компоненты, остатки и движения |
| `/production` | Изделия и BOM |
| `/procurement` | Закупки |
| `/manufacturing/orders` | Производственные заявки |
| `/tasks` | Автоматические и ручные задачи |

## Документация

Общая документация интерфейса и процесса находится в соседнем проекте:

- `smart-factory-ui/docs/SYSTEM.md`;
- `smart-factory-ui/docs/USER_GUIDE.md`;
- `smart-factory-ui/docs/DEVELOPMENT.md`.

Технические документы backend находятся в каталоге [docs](docs).

## Важно

`init_db.py` полностью удаляет таблицы перед созданием схемы. Не запускайте его на рабочей базе. Для изменения структуры используйте Alembic.
