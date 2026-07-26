# Модель данных

Источник истины — SQLAlchemy-модели в `app/models` и миграции `alembic/versions`.

## Основные сущности

| Сущность | Назначение |
|---|---|
| `User` | Сотрудник, основная и дополнительные роли |
| `Component` | Справочник комплектующих |
| `Stock` | Остаток компонента или готового изделия |
| `InventoryMovement` | Неизменяемая запись прихода, расхода или корректировки |
| `ProductType` | Карточка изделия, децимальный номер, чек-лист |
| `ProductBOM` | Иерархический состав изделия |
| `BOMItemAlternative` | Допустимые замены компонента |
| `Order`, `OrderItem` | Производственная заявка и позиции |
| `Reservation` | Резерв компонента под заявку |
| `Item` | Физический экземпляр с заводским номером |
| `WorkflowTask` | Автоматическая или ручная задача |
| `WorkflowBatch` | Партия внутри накопительной задачи |
| `TaskQuantity` | Нормализованный учёт количеств задачи |
| `MaterialTransfer` | Передача компонентов между складом и исполнителем |
| `PurchaseOrder`, `PurchaseItem` | Закупка дефицита |
| `TaskEvent` | Аудит действий по задаче |
| `OrderCancellationObligation` | Обязательство для безопасной отмены |

## Связи

```text
Order 1 ── N OrderItem ── 1 ProductType
Order 1 ── N WorkflowTask
Order 1 ── N Item

ProductType 1 ── N ProductBOM
ProductType 1 ── N Item

WorkflowTask 1 ── N WorkflowBatch
WorkflowTask 1 ── N TaskEvent
WorkflowTask 1 ── N InventoryMovement

Component 1 ── 1 Stock
Component 1 ── N InventoryMovement
```

## Количества

`Stock.actual_qty` — физический остаток. `Stock.reserved_qty` — часть остатка, закреплённая под заявки. Доступное количество вычисляется как:

```text
available = actual_qty - reserved_qty
```

Изменение `actual_qty` должно сопровождаться `InventoryMovement` с направлением и балансом после операции.

## Экземпляры

`Item` связывает заявку, позицию заказа, изделие и заводской номер. Временные поля фиксируют начало сборки, завершение сборки, тест, упаковку и оприходование.

## Миграции

Цепочка миграций линейная. Новая миграция должна ссылаться `down_revision` на текущую head и иметь рабочий `downgrade`.

```bash
alembic upgrade head
```

`Base.metadata.create_all()` допустим для пустой локальной базы, но не заменяет миграции существующей production-базы.
