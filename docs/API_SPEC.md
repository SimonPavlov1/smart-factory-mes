# API Smart Factory MES

Актуальная интерактивная спецификация генерируется FastAPI:

- Swagger UI: `http://127.0.0.1:8000/docs`;
- OpenAPI: `http://127.0.0.1:8000/openapi.json`.

Все защищённые методы принимают заголовок:

```http
Authorization: Bearer <token>
```

## Авторизация и сотрудники

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/auth/login` | Получить токен |
| GET | `/auth/me` | Текущий пользователь, роли и права |
| GET | `/users` | Доступные сотрудники |
| GET/POST | `/admin/users` | Управление сотрудниками |
| PUT/DELETE | `/admin/users/{user_id}` | Изменение или удаление сотрудника |

## Склад

Префикс: `/inventory`.

| Метод | Путь | Назначение |
|---|---|---|
| GET/POST | `/components` | Список или создание компонента |
| GET | `/components/page` | Пагинированный каталог |
| GET | `/components/search` | Поиск |
| GET | `/components/categories` | Категории |
| GET/PUT/DELETE | `/components/{id}` | Карточка и изменение |
| POST | `/incoming` | Приход компонента |
| PATCH | `/components/{id}/quantity` | Ручная корректировка с движением |
| GET | `/movements` | История движений |
| GET | `/finished-goods` | Остатки готовой продукции |
| POST | `/finished-goods/{product_id}/issue` | Выдача готовой продукции |

## Изделия и BOM

Префикс: `/production`.

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/products` | База изделий |
| POST | `/setup-product` | Создать изделие со структурой |
| PUT/DELETE | `/products/{id}` | Изменить или удалить |
| POST | `/products/{id}/photo` | Фото |
| POST | `/products/{id}/attachments` | Документы |
| POST | `/process-bom/{id}` | Добавить BOM |
| POST | `/products/{id}/resolve-bom` | Автоматическое сопоставление |
| GET | `/bom-items/{id}/match-candidates` | Кандидаты детали |
| PUT/DELETE | `/bom-items/{id}` | Строка BOM |
| POST/PUT/DELETE | `/bom-items/{id}/alternatives...` | Альтернативные компоненты |

## Производственные заявки

Префикс: `/manufacturing`.

| Метод | Путь | Назначение |
|---|---|---|
| GET/POST | `/orders` | Список или создание заявки |
| GET/DELETE | `/orders/{id}` | Карточка или удаление |
| POST | `/orders/{id}/issue-materials` | Выдача материалов |
| GET | `/orders/{id}/bom-summary` | Сводка BOM |
| GET | `/orders/{id}/bom-summary.xlsx` | Ведомость XLSX |
| GET | `/orders/{id}/shortages.xlsx` | Дефицит XLSX |

Отмена:

| Метод | Путь |
|---|---|
| POST | `/manufacturing/orders/{id}/cancellation/request` |
| GET | `/manufacturing/orders/{id}/cancellation` |
| POST | `/manufacturing/orders/{id}/cancellation/approve` |
| POST | `/manufacturing/orders/{id}/cancellation/obligations/{obligation_id}/resolve` |

## Задачи

Префикс: `/tasks`.

Основные методы:

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/` | Все задачи для руководителя |
| GET | `/mine` | Доступная пользователю очередь |
| GET | `/{id}` | Полная карточка |
| POST | `/manual` | Ручная задача |
| POST | `/{id}/take` | Взять в работу |
| POST | `/{id}/complete` | Завершить или передать часть |
| POST | `/{id}/assign` | Назначить |
| POST | `/{id}/hold`, `/{id}/resume` | Приостановить или продолжить |
| POST | `/{id}/deadline` | Изменить срок с причиной |
| POST | `/{id}/notes` | Комментарий |
| GET | `/{id}/events` | Аудит событий |
| POST | `/{id}/testing-claims` | Закрепить устройства тестировщика |
| POST | `/{id}/assembly-claims` | Закрепить устройства сборщика |

Для мутаций workflow frontend передаёт ключ идемпотентности. Повторная команда с тем же ключом возвращает сохранённый результат.

## Ошибки

| Код | Значение |
|---|---|
| 400 | Бизнес-правило не выполнено |
| 401 | Нет или истёк токен |
| 403 | Недостаточно прав |
| 404 | Сущность не найдена |
| 409 | Конфликт параллельной работы |
| 422 | Ошибка входных данных |
| 500 | Необработанная ошибка сервера |
