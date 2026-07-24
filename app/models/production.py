from sqlalchemy import Column, Integer, String, Boolean, Text, Float, ForeignKey, JSON, func, and_, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql.sqltypes import DateTime
from app.database import Base


class BOMMapping(Base):
    """
    Глобальная база знаний сопоставлений.
    Хранит исторические данные: какой текст из ПЭ3 соответствует какому ID в базе.
    Позволяет системе автоматически подставлять детали при повторном вводе тех же позиций.
    """
    __tablename__ = "bom_mappings"

    id = Column(Integer, primary_key=True)
    design_name = Column(String, unique=True, index=True, nullable=False,
                         comment="Строка-ключ (напр. 'Конденсатор ССО603...'). Основа для поиска.")

    component_id = Column(Integer, ForeignKey("components.id"), nullable=True,
                          comment="Ссылка на складскую позицию. NULL, если сопоставление не выполнено.")

    mapping_type = Column(String, default="auto",
                          comment="Тип привязки: 'auto' (робот) или 'manual' (человек).")

    is_verified = Column(Boolean, default=False,
                         comment="Подтверждено ли сопоставление ответственным лицом.")

    component = relationship("Component")


class ProductType(Base):
    """
    Реестр выпускаемой продукции и узлов.
    Здесь хранятся 'карточки' изделий, которые компания умеет собирать (Платы, Блоки, Приборы).
    """
    __tablename__ = "product_types"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, comment="Понятное имя (напр. 'Плата управления двигателем')")
    sku = Column(String, unique=True, index=True, comment="Внутренний код/артикул готового товара")
    drawing_number = Column(String, index=True, nullable=True, comment="Децимальный номер по ГОСТ (напр. РСДТ...)")

    is_subassembly = Column(Boolean, default=False,
                            comment="True, если это не готовый продукт, а вложенный узел (полуфабрикат).")

    revision = Column(String, default="1.0", comment="Версия КД или ревизия печатной платы.")
    bill_of_materials_url = Column(String, nullable=True, comment="Путь к чертежам, PDF или исходникам проекта.")
    photo_url = Column(String, nullable=True, comment="Фото или рендер изделия для карточки")
    attachments = Column(JSON, default=list, comment="Файлы КД, сборочные чертежи, составы и прочая документация")
    test_checklist = Column(JSON, default=list, comment="Пункты проверки изделия для задач тестирования")
    description = Column(Text, nullable=True, comment="Технические особенности или нюансы сборки.")

    # Связи
    components = relationship("ProductBOM", back_populates="product", cascade="all, delete-orphan")

    # Связь с позициями заказов, где фигурирует данное изделие
    order_items = relationship("OrderItem", back_populates="product")


class ProductBOM(Base):
    """
    Спецификация состава изделия (BOM).
    Описывает 'рецепт' сборки: какие детали и в каком количестве нужны на 1 единицу.
    """
    __tablename__ = "product_boms"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("product_types.id"), comment="Владелец спецификации")
    parent_id = Column(Integer, ForeignKey("product_boms.id"), nullable=True, index=True,
                       comment="Родительская строка состава для древовидной структуры")

    design_name = Column(String, nullable=False, index=True,
                         comment="Текст из ПЭ3. Позволяет хранить состав даже без привязки к складу.")

    designators = Column(String, nullable=True,
                         comment="Адреса на плате (напр. R1, R5-R10). Важно для монтажников.")

    resource_id = Column(Integer, nullable=True, index=True,
                         comment="ID ресурса. Ссылается на Component или ProductType (зависит от типа).")

    resource_type = Column(String, nullable=False, default="component",
                           comment="Дискриминатор: 'component' (покупное) или 'product' (свой узел).")
    item_type = Column(String, nullable=False, default="component",
                       comment="'assembly', 'component' или 'operation'")
    operation_role = Column(String, nullable=True, comment="Роль/участок для выполнения работы")
    sort_order = Column(Integer, nullable=False, default=0, comment="Порядок строки внутри родителя")

    quantity = Column(Float, nullable=False, default=1.0, comment="Количество на 1 шт. готового изделия.")

    is_resolved = Column(Boolean, default=False,
                         comment="Маркер готовности: найдена ли деталь в складской базе.")

    product = relationship("ProductType", back_populates="components", foreign_keys=[product_id])

    # Полиморфные связи для доступа к данным ресурса
    resource_component = relationship(
        "Component",
        primaryjoin="and_(ProductBOM.resource_id==Component.id, ProductBOM.resource_type=='component')",
        foreign_keys=[resource_id],
        viewonly=True,
        overlaps="resource_product"
    )
    resource_product = relationship(
        "ProductType",
        primaryjoin="and_(ProductBOM.resource_id==ProductType.id, ProductBOM.resource_type=='product')",
        foreign_keys=[resource_id],
        viewonly=True,
        overlaps="resource_component"
    )
    alternatives = relationship("BOMItemAlternative", back_populates="bom_item", cascade="all, delete-orphan")


class BOMItemAlternative(Base):
    """
    Разрешенные складские аналоги для строки состава.
    Например одна строка BOM 'Резистор 10 кОм 1% 0603' может разрешать Samsung, Yageo и Vishay.
    """
    __tablename__ = "bom_item_alternatives"

    id = Column(Integer, primary_key=True)
    bom_item_id = Column(Integer, ForeignKey("product_boms.id", ondelete="CASCADE"), nullable=False, index=True)
    component_id = Column(Integer, ForeignKey("components.id"), nullable=False, index=True)
    is_primary = Column(Boolean, default=False, nullable=False)
    note = Column(String, nullable=True)

    bom_item = relationship("ProductBOM", back_populates="alternatives")
    component = relationship("Component")


class Order(Base):
    """
    Заказ на производство (Производственное задание).
    Хранит общую информацию: заказчик, статус выполнения проекта и дату.
    Содержит в себе вложенный список изделий и объемов через таблицу OrderItem.
    """
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    customer_name = Column(String, nullable=False, comment="Наименование заказчика / контрагента.")
    status = Column(String, default="In Progress", index=True,
                    comment="Этап: In Progress -> In Production -> Completed.")
    created_at = Column(DateTime, server_default=func.now(), comment="Время постановки в очередь.")
    planned_delivery_date = Column(DateTime, nullable=True, comment="Плановая дата поставки заказчику")

    # Связи
    # lazy="joined" автоматически подгружает список позиций при базовом запросе к заказу
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan", lazy="joined")
    reservations = relationship("Reservation", back_populates="order")
    items_sn = relationship("Item", back_populates="order")


class OrderItem(Base):
    """
    Позиции производственного заказа.
    Связывает один комплексный заказ с несколькими позициями каталога изделий (ProductType).
    """
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False,
                      comment="Ссылка на главный заказ")
    product_id = Column(Integer, ForeignKey("product_types.id"), nullable=False, comment="Ссылка на собираемое изделие")
    quantity = Column(Integer, nullable=False, default=1, comment="План выпуска данного изделия (штук).")

    # Обратные связи
    order = relationship("Order", back_populates="items")
    product = relationship("ProductType", back_populates="order_items")


class Reservation(Base):
    """
    Система бронирования остатков.
    Виртуально 'замораживает' детали на складе под конкретный производственный заказ.
    """
    __tablename__ = "reservations"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), index=True, nullable=False)
    component_id = Column(Integer, ForeignKey("components.id"), index=True, nullable=False)
    qty = Column(Float, nullable=False, comment="Количество забронированных единиц.")

    order = relationship("Order", back_populates="reservations")


class Item(Base):
    """
    Учет готовой продукции по серийным номерам.
    Запись о физически собранном экземпляре изделия.
    """
    __tablename__ = "items"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), index=True, nullable=False)
    serial_number = Column(String, unique=True, index=True, nullable=False, comment="SN изделия.")
    test_result = Column(String, nullable=True, comment="Результат прохождения ОТК.")

    order = relationship("Order", back_populates="items_sn")


class WorkflowTask(Base):
    """
    Задача производственного workflow.
    Создается системой при переходах заказа между ролями.
    """
    __tablename__ = "workflow_tasks"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), index=True, nullable=True)
    type = Column(String, index=True, nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    role = Column(String, index=True, nullable=False)
    status = Column(String, default="assigned", index=True, nullable=False)
    assigned_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    payload = Column(JSON, nullable=True)
    sort_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    due_date = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class MaterialTransfer(Base):
    """Физическая передача компонентов со склада конкретному подразделению."""
    __tablename__ = "material_transfers"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    source_task_id = Column(Integer, ForeignKey("workflow_tasks.id"), nullable=True, index=True)
    issue_task_id = Column(Integer, ForeignKey("workflow_tasks.id"), nullable=False, unique=True, index=True)
    receive_task_id = Column(Integer, ForeignKey("workflow_tasks.id"), nullable=True, unique=True, index=True)
    source_task_type = Column(String, nullable=True, index=True)
    recipient_role = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="reserved", index=True)
    issued_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    accepted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    issued_at = Column(DateTime, nullable=True)
    accepted_at = Column(DateTime, nullable=True)

    lines = relationship("MaterialTransferLine", back_populates="transfer", cascade="all, delete-orphan")


class MaterialTransferLine(Base):
    """Количества одной складской позиции на каждом этапе передачи."""
    __tablename__ = "material_transfer_lines"

    id = Column(Integer, primary_key=True, index=True)
    transfer_id = Column(Integer, ForeignKey("material_transfers.id", ondelete="CASCADE"), nullable=False, index=True)
    component_id = Column(Integer, ForeignKey("components.id"), nullable=False, index=True)
    line_uid = Column(String, nullable=True, index=True)
    requested_qty = Column(Float, nullable=False, default=0)
    reserved_qty = Column(Float, nullable=False, default=0)
    issued_qty = Column(Float, nullable=False, default=0)
    accepted_qty = Column(Float, nullable=False, default=0)

    transfer = relationship("MaterialTransfer", back_populates="lines")


class TaskQuantity(Base):
    """Нормализованные количества задачи; JSON используется только как снимок для UI."""
    __tablename__ = "task_quantities"
    __table_args__ = (
        UniqueConstraint("task_id", "entity_type", "entity_id", "line_uid", name="uq_task_quantity_line"),
    )

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("workflow_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=True, index=True)
    entity_type = Column(String, nullable=False, index=True)  # component / product
    entity_id = Column(Integer, nullable=False, index=True)
    line_uid = Column(String, nullable=False, default="", index=True)
    requested_qty = Column(Float, nullable=False, default=0)
    reserved_qty = Column(Float, nullable=False, default=0)
    purchased_qty = Column(Float, nullable=False, default=0)
    received_qty = Column(Float, nullable=False, default=0)
    issued_qty = Column(Float, nullable=False, default=0)
    accepted_qty = Column(Float, nullable=False, default=0)
    processed_qty = Column(Float, nullable=False, default=0)
    rejected_qty = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class MaterialBatch(Base):
    """Партия закупки/приемки либо выпуска, больше не спрятанная в payload задачи."""
    __tablename__ = "material_batches"
    __table_args__ = (
        UniqueConstraint("source_task_id", "external_key", name="uq_material_batch_source_key"),
    )

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    source_task_id = Column(Integer, ForeignKey("workflow_tasks.id"), nullable=False, index=True)
    batch_type = Column(String, nullable=False, index=True)  # purchase / receipt / test / packing / finished_goods
    external_key = Column(String, nullable=False, default="default")
    status = Column(String, nullable=False, default="recorded", index=True)
    document_ref = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    lines = relationship("MaterialBatchLine", back_populates="batch", cascade="all, delete-orphan")


class MaterialBatchLine(Base):
    __tablename__ = "material_batch_lines"
    __table_args__ = (
        UniqueConstraint("batch_id", "entity_type", "entity_id", "line_uid", name="uq_material_batch_line"),
    )

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("material_batches.id", ondelete="CASCADE"), nullable=False, index=True)
    entity_type = Column(String, nullable=False, index=True)
    entity_id = Column(Integer, nullable=False, index=True)
    line_uid = Column(String, nullable=False, default="")
    quantity = Column(Float, nullable=False)
    rejected_qty = Column(Float, nullable=False, default=0)

    batch = relationship("MaterialBatch", back_populates="lines")


class WorkflowCommand(Base):
    """Результат команды для безопасного повтора HTTP-запроса."""
    __tablename__ = "workflow_commands"
    __table_args__ = (
        UniqueConstraint("task_id", "idempotency_key", name="uq_workflow_command_task_key"),
    )

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("workflow_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    idempotency_key = Column(String, nullable=False)
    command_type = Column(String, nullable=False, default="complete")
    request_hash = Column(String, nullable=False)
    response_payload = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class WorkflowBatch(Base):
    """Входящая производственная партия внутри накопительной задачи."""
    __tablename__ = "workflow_batches"
    __table_args__ = (
        UniqueConstraint("container_task_id", "batch_key", name="uq_workflow_batch_container_key"),
    )

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    container_task_id = Column(Integer, ForeignKey("workflow_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    source_task_id = Column(Integer, ForeignKey("workflow_tasks.id"), nullable=True, index=True)
    stage = Column(String, nullable=False, index=True)
    cycle = Column(String, nullable=False, default="primary", index=True)
    batch_key = Column(String, nullable=False)
    status = Column(String, nullable=False, default="queued", index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    processed_at = Column(DateTime, nullable=True)

    lines = relationship("WorkflowBatchLine", back_populates="batch", cascade="all, delete-orphan")


class WorkflowBatchLine(Base):
    __tablename__ = "workflow_batch_lines"

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("workflow_batches.id", ondelete="CASCADE"), nullable=False, index=True)
    entity_type = Column(String, nullable=False, index=True)
    entity_id = Column(Integer, nullable=False, index=True)
    quantity = Column(Float, nullable=False)
    processed_qty = Column(Float, nullable=False, default=0)
    rejected_qty = Column(Float, nullable=False, default=0)

    batch = relationship("WorkflowBatch", back_populates="lines")
