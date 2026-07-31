import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import lazyload, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.inventory import Component, Stock
from app.models.production import (
    Item,
    MaterialTransfer,
    MaterialTransferLine,
    Order,
    OrderItem,
    ProductBOM,
    ProductType,
    Reservation,
    WorkflowTask,
)
from app.services.factory_number_service import create_product_units
from app.services.order_adjustment_service import apply_order_adjustment, preview_order_adjustment
from app.services.workflow_service import complete_task, create_task, ensure_missing_order_item_workflows


class OrderQuantityAdjustmentTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        self.component = Component(name="Компонент", part_number="CMP-ADJ")
        self.product = ProductType(name="Устройство", sku="DEVICE-ADJ")
        self.order = Order(customer_name="Заказчик", status="In Assembly")
        self.db.add_all([self.component, self.product, self.order])
        self.db.flush()
        self.db.add(ProductBOM(
            product_id=self.product.id,
            design_name="Компонент",
            resource_id=self.component.id,
            resource_type="component",
            item_type="component",
            quantity=2,
            is_resolved=True,
        ))
        self.order_item = OrderItem(order_id=self.order.id, product_id=self.product.id, quantity=10)
        self.stock = Stock(component_id=self.component.id, actual_qty=100, reserved_qty=20)
        self.db.add_all([self.order_item, self.stock])
        self.db.flush()
        self.db.add(Reservation(order_id=self.order.id, component_id=self.component.id, qty=20))
        self.assembly = WorkflowTask(
            order_id=self.order.id,
            product_id=self.product.id,
            type="assembler_build",
            title="Сборка",
            role="assembler",
            status="in_progress",
            payload={
                "product_context": {
                    "order_item_id": self.order_item.id,
                    "product_id": self.product.id,
                    "product_name": self.product.name,
                    "qty": 10,
                },
                "planned_qty": 10,
            },
        )
        self.db.add(self.assembly)
        self.db.flush()

    def tearDown(self):
        self.db.rollback()
        self.db.close()
        self.engine.dispose()

    def _units(self, quantity=10):
        return create_product_units(
            self.db,
            order_id=self.order.id,
            order_item_id=self.order_item.id,
            product=self.product,
            assembly_task_id=self.assembly.id,
            assigned_user_id=None,
            quantity=quantity,
        )

    def test_preview_releases_stocked_surplus(self):
        units = self._units(8)
        for unit in units:
            unit.status = "stocked"
        preview = preview_order_adjustment(self.db, self.order, {self.order_item.id: 6})
        line = preview["lines"][0]
        self.assertEqual(line["surplus_to_free_stock"], 2)
        self.assertEqual(line["locked_wip_qty"], 0)

    def test_decrease_below_work_in_progress_is_rejected(self):
        units = self._units(7)
        for unit in units:
            unit.status = "assembled"
        with self.assertRaises(HTTPException) as error:
            preview_order_adjustment(self.db, self.order, {self.order_item.id: 6})
        self.assertEqual(error.exception.status_code, 409)

    def test_decrease_marks_surplus_and_releases_reservation(self):
        units = self._units(8)
        for unit in units:
            unit.status = "stocked"
        result = apply_order_adjustment(
            self.db,
            self.order,
            {self.order_item.id: 6},
            reason="Клиент уменьшил заказ",
            actor_user_id=10,
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.order_item.quantity, 6)
        self.assertEqual(self.db.query(Item).filter(Item.is_order_surplus.is_(True)).count(), 2)
        self.assertEqual(self.db.query(Reservation).filter_by(order_id=self.order.id).one().qty, 12)
        self.assertEqual(self.stock.reserved_qty, 12)
        self.assertEqual(len(self.order.adjustment_history), 1)

    def test_increase_updates_plan_reservation_and_device_pool(self):
        self._units(10)
        apply_order_adjustment(
            self.db,
            self.order,
            {self.order_item.id: 12},
            reason="Дополнительное соглашение",
            actor_user_id=10,
        )
        self.assertEqual(self.order_item.quantity, 12)
        self.assertEqual(self.db.query(Reservation).filter_by(order_id=self.order.id).one().qty, 24)
        self.assertEqual(self.stock.reserved_qty, 24)
        self.assertEqual(self.db.query(Item).filter_by(order_item_id=self.order_item.id).count(), 12)
        self.assertEqual(self.assembly.payload["planned_qty"], 12)
        self.assertEqual(self.assembly.payload["product_context"]["qty"], 12)

    def test_issued_excess_creates_return_task_and_completion_restocks(self):
        self._units(6)
        transfer = MaterialTransfer(
            order_id=self.order.id,
            issue_task_id=self.assembly.id,
            recipient_role="assembler",
            status="accepted",
        )
        transfer.lines.append(MaterialTransferLine(
            component_id=self.component.id,
            requested_qty=20,
            reserved_qty=20,
            issued_qty=20,
            accepted_qty=20,
        ))
        self.db.add(transfer)
        self.db.flush()
        before_stock = self.stock.actual_qty

        result = apply_order_adjustment(
            self.db,
            self.order,
            {self.order_item.id: 6},
            reason="Сокращение заказа",
            actor_user_id=10,
        )
        self.assertEqual(result["return_materials_total"], 8)
        return_task = self.db.query(WorkflowTask).filter_by(
            order_id=self.order.id,
            type="order_adjustment_return",
        ).one()
        self.assertEqual(return_task.status, "assigned")

        completion = complete_task(
            self.db,
            return_task,
            {"items": [{"component_id": self.component.id, "qty": 8}]},
            actor_user_id=20,
        )
        self.assertEqual(completion["status"], "done")
        self.assertEqual(return_task.status, "done")
        self.assertEqual(self.stock.actual_qty, before_stock + 8)

    def test_postgresql_order_lock_does_not_join_nullable_order_items(self):
        statement = (
            self.db.query(Order)
            .options(lazyload(Order.items))
            .filter(Order.id == self.order.id)
            .with_for_update()
            .limit(1)
            .statement
        )
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("FOR UPDATE", sql)
        self.assertNotIn("JOIN order_items", sql)

    def test_bom_change_is_added_to_existing_procurement_task(self):
        first_missing = Component(name="Первая забытая позиция", part_number="MISS-1")
        second_missing = Component(name="Вторая забытая позиция", part_number="MISS-2")
        self.db.add_all([first_missing, second_missing])
        self.db.flush()
        self.db.add(ProductBOM(
            product_id=self.product.id,
            design_name=first_missing.name,
            resource_id=first_missing.id,
            resource_type="component",
            item_type="component",
            quantity=1,
            is_resolved=True,
        ))
        procurement = create_task(
            self.db,
            order_id=self.order.id,
            task_type="procurement_purchase",
            title="Закупить комплектующие",
            role="procurement",
            payload={"shortages": [{"component_id": first_missing.id, "qty": 10, "shortage_qty": 10}]},
        )
        self.db.flush()

        # The order item already has workflow tasks; only its BOM changed.
        self.db.add(ProductBOM(
            product_id=self.product.id,
            design_name=second_missing.name,
            resource_id=second_missing.id,
            resource_type="component",
            item_type="component",
            quantity=2,
            is_resolved=True,
        ))
        self.db.flush()
        ensure_missing_order_item_workflows(self.db)

        self.assertEqual(
            {line["component_id"] for line in procurement.payload["shortages"]},
            {first_missing.id, second_missing.id},
        )


if __name__ == "__main__":
    unittest.main()
