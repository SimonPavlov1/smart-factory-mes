# Спецификация структуры данных Smart Factory

## 1. Слой моделей: Модуль `inventory.py`

Назначение: Управление товарно-материальными ценностями (ТМЦ).

| **Сущность**  | **Поле**         | **Тип SQL** | **Ограничения** | **Описание**                        |
|---------------|------------------|-------------|-----------------|-------------------------------------|
| **Component** | `id`             | Integer     | PK, Index       | Уникальный ID детали                |
| (Справочник)  | `name`           | String      | Not Null        | Название (напр. Резистор)           |
|               | `part_number`    | String      | Unique, Index   | Артикул производителя               |
|               | `category`       | String      | Index           | Группа (IC, Resistors и т.д.)       |
|               | `type`           | String      | -               | Подтип (напр. MLCC, Тонкопленочный) |
|               | `package`        | String      | Index           | Корпус (0603, SOT-23)               |
|               | `value`          | String      | -               | Номинал (10k, 100nF)                |
|               | `tolerance`      | String      | -               | Допуск/Точность (1%, X7R)           |
|               | `unit`           | String      | Default="pcs"   | Ед. измерения (шт, м, кг)           |
|               | `description`    | Text        | Nullable        | Доп. информация                     |
|               | `datasheet_path` | String      | Nullable        | Путь к PDF-файлу документации       |
| **Stock**     | `id`             | Integer     | PK              | ID записи остатка                   |
| (Склад)       | `component_id`   | Integer     | FK, Unique      | Связь с Component                   |
|               | `actual_qty`     | Float       | Default=0.0     | Физическое наличие на полке         |
|               | `reserved_qty`   | Float       | Default=0.0     | Забронировано под заказы            |
|               | `location`       | String      | Index           | Место на полке                      |
---
## 2. Слой моделей: Модуль `production.py`

Назначение: Управление производственным циклом и конструкторской документацией.

| **Сущность**     | **Поле**                | **Тип SQL** | **Ограничения** | **Описание**                            |
|------------------|-------------------------|-------------|-----------------|-----------------------------------------|
| **ProductType**  | `id`                    | Integer     | PK, Index       | Уникальный ID изделия                   |
| (Реестр изделий) | `name`                  | String      | Not Null        | Коммерческое название прибора           |
|                  | `sku`                   | String      | Unique, Index   | Внутренний артикул (напр. РСДТ.123.123) |
|                  | `is_subassembly`        | Boolean     | Default=False   | Признак узла (плата, кабель питания...) |
|                  | `revision`              | String      | Default="1.0"   | Версия конструкторской документации     |
|                  | `bill_of_materials_url` | Nullable    | Default="1.0"   | Ссылка на полный пакет КД               |
|                  | `description`           | Text        | Nullable        | Описание функционала или ТУ             |
| **ProductBOM**   | `id`                    | Integer     | PK              | ID строки спецификации                  |
| (Спецификация)   | `product_id`            | Integer     | FK              | Ссылка на родительское изделие          |
|                  | `resource_id`           | Integer     | Not Null        | ID того, что берем (деталь или узел)    |
|                  | `resource_type`         | String      | Not Null        | Маркер: 'component' или 'subassembly'   |
|                  | `quantity`              | Float       | Not Null        | Кол-во на 1 ед. (шт, метры)             |
|                  | `designators`           | String      | Nullable        | Поз. обозначения на плате (R1, C5)      |
| **Order**        | `id`                    | Integer     | PK, Index       | Номер заказа на сборку                  |
| (Заказы)         | `product_id`            | Integer     | FK              | Что именно собираем                     |
|                  | `target_qty`            | Integer     | Not Null        | План выпуска (кол-во шт. в партии)      |
|                  | `status`                | String      | Index           | Статус (New, In Progress, Done)         |
|                  | `created_at`            | DateTime    | func.now()      | Дата и время постановки в план          |
| **Reservation**  | `id`                    | Integer     | PK              | ID записи брони                         |
| (Резервы)        | `order_id`              | Integer     | FK              | Под какой заказ заняты детали           |
|                  | `component_id`          | Integer     | FK              | Какая покупная деталь заблокирована     |
|                  | `qty`                   | Float       | Not Null        | Количество забронированных единиц       |
| **Item**         | `id`                    | Integer     | PK              | ID конкретного прибора                  |
| (Экземпляры)     | `order_id`              | Integer     | FK              | Из какого заказа этот прибор            |
|                  | `serial_number`         | String      | Unique, Index   | Уникальный серийный номер прибора       |
|                  | `test_result`           | String      | Nullable        | Результат финального ОТК (Pass/Fail)    |