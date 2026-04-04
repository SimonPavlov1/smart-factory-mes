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