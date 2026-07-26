import unittest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.auth import User  # noqa: F401
from app.models.inventory import Component, InventoryMovement, Stock
from app.models.procurement import PurchaseItem, PurchaseOrder  # noqa: F401
from app.models.production import (
    Item, MaterialBatch, MaterialTransfer, Order, OrderItem, ProductType,
    Reservation, TaskQuantity, WorkflowBatch, WorkflowTask,
)
from app.services.order_progress_service import aggregate_order_progress
from app.services.workflow_service import complete_task, create_initial_order_tasks, create_task
from app.api.tasks import (
    AssemblyAllocationPayload,
    AssemblyPlanPayload,
    TaskCompletePayload,
    complete_workflow_task,
    create_assembly_plan,
)
from app.services.workflow_batch_service import batch_summary, pending_product_lines
from app.time_utils import utcnow


class FullOrderCycleTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        self.product = ProductType(name="УТУД-10", sku="UTUD-10")
        self.order = Order(customer_name="Сценарный тест")
        self.db.add_all([self.product, self.order])
        self.db.flush()
        self.item = OrderItem(order_id=self.order.id, product_id=self.product.id, quantity=10)
        self.db.add(self.item)
        self.db.flush()
        self.context = {
            "order_item_id": self.item.id, "product_id": self.product.id,
            "product_name": self.product.name, "drawing_number": None, "qty": 10,
        }

    def tearDown(self):
        self.db.rollback()
        self.db.close()
        self.engine.dispose()

    def _take_and_complete(self, task, payload):
        task.status = "in_progress"
        return complete_task(self.db, task, payload, actor_user_id=None)

    def _latest(self, task_type, status=None):
        query = self.db.query(WorkflowTask).filter_by(order_id=self.order.id, type=task_type)
        if status:
            query = query.filter_by(status=status)
        return query.order_by(WorkflowTask.id.desc()).first()

    def test_order_with_defect_repair_retest_and_two_finished_batches(self):
        assembly = create_task(
            self.db, order_id=self.order.id, task_type="assembler_build",
            title="Собрать", role="assembler",
            payload={"product_context": self.context, "product_lines": [self.context], "materials_complete": True},
        )
        self._take_and_complete(assembly, {"assembled_qty": 10})

        first_test = self._latest("tester_check")
        self._take_and_complete(first_test, {
            "passed_qty": 9,
            "defective_qty": 1,
            "defective_products": [{"product_id": self.product.id, "defective_qty": 1}],
        })
        first_pack = self._latest("packer_pack")
        repair = self._latest("repair_defects")
        self._take_and_complete(first_pack, {"packed_qty": 9})
        first_finished = self._latest("warehouse_finished_goods")
        self._take_and_complete(first_finished, {
            "accepted_goods": [{"product_id": self.product.id, "qty": 9}],
        })

        self._take_and_complete(repair, {"notes": "Дефект устранен"})
        retest = self._latest("tester_check", "assigned")
        self.assertEqual(retest.id, first_test.id)
        self.assertEqual(self.db.query(WorkflowTask).filter_by(type="tester_check").count(), 1)
        self.assertEqual(retest.payload["retest_serial_numbers"], repair.payload.get("serial_numbers", []))
        self._take_and_complete(retest, {"passed_qty": 1, "defective_qty": 0})
        second_pack = self._latest("packer_pack", "assigned")
        self.assertEqual(second_pack.id, first_pack.id)
        self._take_and_complete(second_pack, {"packed_qty": 1})
        second_finished = self._latest("warehouse_finished_goods", "assigned")
        self.assertEqual(second_finished.id, first_finished.id)
        self._take_and_complete(second_finished, {
            "accepted_goods": [{"product_id": self.product.id, "qty": 1}],
        })

        stock = self.db.query(Stock).filter_by(product_id=self.product.id).one()
        self.assertEqual(stock.actual_qty, 10)
        self.assertEqual(self.order.status, "Ready To Ship")
        progress = aggregate_order_progress(self.db, self.order)
        self.assertEqual(progress["state"], "completed")
        self.assertEqual(progress["finished_qty"], 10)
        self.assertEqual(progress["percent"], 100)
        self.assertEqual(self.db.query(WorkflowTask).filter_by(type="packer_pack").count(), 1)
        self.assertEqual(self.db.query(WorkflowTask).filter_by(type="warehouse_finished_goods").count(), 1)
        self.assertEqual(self.db.query(WorkflowBatch).filter_by(stage="packer_pack").count(), 2)
        self.assertEqual(self.db.query(WorkflowBatch).filter_by(stage="warehouse_finished_goods").count(), 2)

    def test_complete_cycle_from_procurement_to_finished_goods(self):
        component = Component(
            name="Микросхема тестовая",
            part_number="FULL-CYCLE-IC",
            category="Микросхемы",
            package="SO-8",
        )
        self.db.add(component)
        self.db.flush()
        self.db.add(Stock(component_id=component.id, actual_qty=0, reserved_qty=0, location="A-01"))
        self.db.flush()

        shortage = {
            "component_id": component.id,
            "line_uid": f"{self.item.id}:{component.id}:0",
            "order_item_id": self.item.id,
            "product_id": self.product.id,
            "product_name": self.product.name,
            "qty": 20,
            "required_qty": 20,
            "shortage_qty": 20,
            "available_qty": 0,
        }
        create_initial_order_tasks(
            self.db,
            self.order,
            materials=[shortage],
            shortages=[shortage],
            product_context=self.context,
        )

        procurement = self._latest("procurement_purchase", "assigned")
        self.assertIsNotNone(procurement)
        procurement_result = self._take_and_complete(procurement, {
            "invoice": "INV-FULL-1",
            "supplier": "Тестовый поставщик",
            "expected_date": "2099-12-31",
            "deliveries": [{
                "component_id": component.id,
                "line_uid": shortage["line_uid"],
                "qty": 20,
            }],
        })
        self.assertEqual(procurement_result["status"], "done")

        payment = self._latest("accounting_payment", "assigned")
        self.assertIsNotNone(payment)
        self._take_and_complete(payment, {"payment_ref": "PAY-FULL-1"})

        warehouse_receipt = self._latest("warehouse_receive_components", "assigned")
        self.assertIsNotNone(warehouse_receipt)
        receipt_line = warehouse_receipt.payload["shortages"][0]
        self._take_and_complete(warehouse_receipt, {
            "items": [{
                "component_id": component.id,
                "line_uid": receipt_line["line_uid"],
                "qty": 20,
            }],
        })

        stock = self.db.query(Stock).filter_by(component_id=component.id).one()
        self.assertEqual(stock.actual_qty, 20)
        self.assertEqual(stock.reserved_qty, 20)
        self.assertEqual(self.db.query(Reservation).filter_by(order_id=self.order.id).count(), 1)

        issue = self._latest("warehouse_issue_materials", "assigned")
        self.assertIsNotNone(issue)
        self._take_and_complete(issue, {})
        self.assertEqual(issue.status, "ready_to_issue")

        material_receipt = self._latest("assembler_receive_materials", "assigned")
        self.assertIsNotNone(material_receipt)
        self._take_and_complete(material_receipt, {})
        self.assertEqual(issue.status, "done")
        transfer = self.db.query(MaterialTransfer).filter_by(issue_task_id=issue.id).one()
        self.assertEqual(transfer.status, "accepted")

        stock = self.db.query(Stock).filter_by(component_id=component.id).one()
        self.assertEqual(stock.actual_qty, 0)
        self.assertEqual(stock.reserved_qty, 0)
        self.assertEqual(self.db.query(Reservation).filter_by(order_id=self.order.id).count(), 0)

        planning = self._latest("assembler_build", "assigned")
        self.assertIsNotNone(planning)
        self.assertTrue(planning.payload["materials_complete"])
        assembly = planning
        self._take_and_complete(assembly, {"assembled_qty": 10})
        units = self.db.query(Item).filter_by(order_id=self.order.id).all()
        self.assertEqual(len(units), 10)
        self.assertEqual(len({unit.serial_number for unit in units}), 10)

        testing = self._latest("tester_check", "assigned")
        self._take_and_complete(testing, {"passed_qty": 10, "defective_qty": 0})

        packing = self._latest("packer_pack", "assigned")
        self._take_and_complete(packing, {"packed_qty": 10})

        finished_goods = self._latest("warehouse_finished_goods", "assigned")
        self._take_and_complete(finished_goods, {
            "accepted_goods": [{"product_id": self.product.id, "qty": 10}],
        })

        self.assertEqual(self.order.status, "Ready To Ship")
        self.assertEqual(
            self.db.query(Stock).filter_by(product_id=self.product.id).one().actual_qty,
            10,
        )
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                order_id=self.order.id,
                component_id=component.id,
                direction="incoming",
            ).count(),
            1,
        )
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                order_id=self.order.id,
                component_id=component.id,
                direction="outgoing",
            ).count(),
            1,
        )
        active_tasks = self.db.query(WorkflowTask).filter(
            WorkflowTask.order_id == self.order.id,
            WorkflowTask.status.notin_(["done", "cancelled", "merged"]),
        ).all()
        self.assertEqual(active_tasks, [])

    def test_partial_stock_is_issued_while_only_missing_components_are_purchased(self):
        available_component = Component(
            name="Резистор со склада",
            part_number="FULL-CYCLE-IN-STOCK",
            category="Резисторы",
            package="0603",
        )
        missing_component = Component(
            name="Конденсатор под закупку",
            part_number="FULL-CYCLE-MISSING",
            category="Конденсаторы",
            package="0603",
        )
        self.db.add_all([available_component, missing_component])
        self.db.flush()
        self.db.add_all([
            Stock(
                component_id=available_component.id,
                actual_qty=10,
                reserved_qty=0,
                location="A-01",
            ),
            Stock(
                component_id=missing_component.id,
                actual_qty=0,
                reserved_qty=0,
                location="A-02",
            ),
        ])
        self.db.flush()

        available_line = {
            "component_id": available_component.id,
            "line_uid": f"{self.item.id}:{available_component.id}:0",
            "order_item_id": self.item.id,
            "product_id": self.product.id,
            "product_name": self.product.name,
            "qty": 10,
            "required_qty": 10,
        }
        missing_line = {
            "component_id": missing_component.id,
            "line_uid": f"{self.item.id}:{missing_component.id}:1",
            "order_item_id": self.item.id,
            "product_id": self.product.id,
            "product_name": self.product.name,
            "qty": 20,
            "required_qty": 20,
            "shortage_qty": 20,
            "available_qty": 0,
        }
        create_initial_order_tasks(
            self.db,
            self.order,
            materials=[available_line, missing_line],
            shortages=[missing_line],
            product_context=self.context,
            available_materials=[available_line],
        )

        available_stock = self.db.query(Stock).filter_by(component_id=available_component.id).one()
        self.assertEqual(available_stock.actual_qty, 10)
        self.assertEqual(available_stock.reserved_qty, 10)
        procurement = self._latest("procurement_purchase", "assigned")
        self.assertEqual(
            [line["component_id"] for line in procurement.payload["shortages"]],
            [missing_component.id],
        )

        first_issue = self._latest("warehouse_issue_materials", "assigned")
        self.assertTrue(first_issue.payload["partial"])
        self.assertEqual(
            [line["component_id"] for line in first_issue.payload["materials"]],
            [available_component.id],
        )
        self._take_and_complete(first_issue, {})
        first_receipt = self._latest("assembler_receive_materials", "assigned")
        self._take_and_complete(first_receipt, {})

        assembly = self._latest("assembler_build", "assigned")
        self.assertIsNotNone(assembly)
        self.assertFalse(assembly.payload["materials_complete"])
        self.assertEqual(
            self.db.query(WorkflowTask).filter_by(
                order_id=self.order.id,
                type="assembler_build",
            ).count(),
            1,
        )

        self._take_and_complete(procurement, {
            "invoice": "INV-PARTIAL-1",
            "supplier": "Тестовый поставщик",
            "expected_date": "2099-12-31",
            "deliveries": [{
                "component_id": missing_component.id,
                "line_uid": missing_line["line_uid"],
                "qty": 20,
            }],
        })
        payment = self._latest("accounting_payment", "assigned")
        self._take_and_complete(payment, {"payment_ref": "PAY-PARTIAL-1"})
        warehouse_receipt = self._latest("warehouse_receive_components", "assigned")
        receipt_line = warehouse_receipt.payload["shortages"][0]
        self._take_and_complete(warehouse_receipt, {
            "items": [{
                "component_id": missing_component.id,
                "line_uid": receipt_line["line_uid"],
                "qty": 20,
            }],
        })

        second_issue = self._latest("warehouse_issue_materials", "assigned")
        self.assertNotEqual(second_issue.id, first_issue.id)
        self.assertEqual(
            [line["component_id"] for line in second_issue.payload["materials"]],
            [missing_component.id],
        )
        self._take_and_complete(second_issue, {})
        second_receipt = self._latest("assembler_receive_materials", "assigned")
        self._take_and_complete(second_receipt, {})

        assembly = self._latest("assembler_build", "assigned")
        self.assertTrue(assembly.payload["materials_complete"])
        self.assertEqual(
            self.db.query(WorkflowTask).filter_by(
                order_id=self.order.id,
                type="assembler_build",
            ).count(),
            1,
        )
        self.assertEqual(
            self.db.query(Stock).filter_by(component_id=available_component.id).one().actual_qty,
            0,
        )
        self.assertEqual(
            self.db.query(Stock).filter_by(component_id=missing_component.id).one().actual_qty,
            0,
        )

    def test_quantities_do_not_confuse_requested_and_purchased(self):
        task = create_task(
            self.db, order_id=self.order.id, task_type="procurement_purchase",
            title="Закупить", role="procurement",
            payload={
                "shortages": [{"component_id": 42, "line_uid": "a", "shortage_qty": 2}],
                "purchases": [{"component_id": 42, "line_uid": "a", "qty": 20}],
            },
        )
        from app.services.quantity_service import sync_task_quantities
        sync_task_quantities(self.db, task)
        rows = self.db.query(TaskQuantity).filter_by(task_id=task.id).all()
        self.assertEqual(sum(row.requested_qty for row in rows), 2)
        self.assertEqual(sum(row.purchased_qty for row in rows), 20)

    def test_repeated_completion_command_returns_saved_result(self):
        user = User(username="assembler", password_hash="x", role="assembler", roles=["assembler"])
        self.db.add(user)
        task = create_task(
            self.db, order_id=self.order.id, task_type="assembler_build",
            title="Собрать", role="assembler",
            payload={"product_context": self.context, "product_lines": [self.context], "materials_complete": True},
        )
        self.db.add(task)
        self.db.commit()
        task.status = "in_progress"
        task.assigned_user_id = user.id
        self.db.commit()
        command = TaskCompletePayload(payload={"assembled_qty": 10}, idempotency_key="same-command")
        first = complete_workflow_task(task.id, command, self.db, user)
        second = complete_workflow_task(task.id, command, self.db, user)
        self.assertEqual(first, second)
        self.assertEqual(self.db.query(WorkflowTask).filter_by(
            order_id=self.order.id, type="tester_check",
        ).count(), 1)

    def test_partial_assembly_accumulates_in_one_testing_task(self):
        assembly = create_task(
            self.db, order_id=self.order.id, task_type="assembler_build",
            title="Собрать", role="assembler",
            payload={"product_context": self.context, "product_lines": [self.context], "materials_complete": True},
        )
        self._take_and_complete(assembly, {"assembled_qty": 3})
        testing = self._latest("tester_check")
        first_result = self._take_and_complete(testing, {"passed_qty": 3, "defective_qty": 0})
        self.assertEqual(first_result["status"], "partial")
        self.assertEqual(testing.status, "assigned")
        testing.status = "in_progress"
        with self.assertRaisesRegex(HTTPException, "нет изделий для проверки"):
            complete_task(self.db, testing, {"passed_qty": 3, "defective_qty": 0})
        testing.status = "assigned"

        self._take_and_complete(assembly, {"assembled_qty": 10})
        testing_tasks = self.db.query(WorkflowTask).filter_by(
            order_id=self.order.id, type="tester_check",
        ).all()
        self.assertEqual(len(testing_tasks), 1)
        self.assertEqual(batch_summary(self.db, testing)["batches_total"], 2)
        self.assertEqual(batch_summary(self.db, testing)["pending_qty"], 7)

    def test_testing_form_shows_all_pending_deliveries(self):
        first = create_task(
            self.db, order_id=self.order.id, task_type="tester_check",
            title="Тест", role="tester",
            payload={"product_context": self.context, "product_lines": [{**self.context, "qty": 3}]},
        )
        second = create_task(
            self.db, order_id=self.order.id, task_type="tester_check",
            title="Тест", role="tester",
            payload={"product_context": self.context, "product_lines": [{**self.context, "qty": 7}]},
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(batch_summary(self.db, first)["pending_qty"], 10)
        self.assertEqual(batch_summary(self.db, first)["current_batch_qty"], 3)
        self.assertEqual(sum(line["qty"] for line in pending_product_lines(self.db, first, aggregate_all=True)), 10)

    def test_completed_packing_task_is_reopened_for_next_batch(self):
        first = create_task(
            self.db, order_id=self.order.id, task_type="packer_pack",
            title="Упаковать", role="packer",
            payload={
                "product_context": self.context,
                "product_lines": [{**self.context, "qty": 2}],
                "unit_ids": [101, 102],
                "serial_numbers": ["UTUD-101", "UTUD-102"],
            },
        )
        first.status = "done"
        first.completed_at = utcnow()
        first.payload = {**first.payload, "completion": {"packed_qty": 2}}

        reopened = create_task(
            self.db, order_id=self.order.id, task_type="packer_pack",
            title="Упаковать следующую партию", role="packer",
            payload={
                "product_context": self.context,
                "product_lines": [{**self.context, "qty": 1}],
                "unit_ids": [103],
                "serial_numbers": ["UTUD-103"],
            },
        )

        self.assertEqual(reopened.id, first.id)
        self.assertEqual(reopened.status, "assigned")
        self.assertIsNone(reopened.completed_at)
        self.assertEqual(reopened.payload["completion"], {})
        self.assertEqual(reopened.payload["completion_history"][-1]["packed_qty"], 2)
        self.assertEqual(reopened.payload["serial_numbers"], ["UTUD-101", "UTUD-102", "UTUD-103"])
        self.assertEqual(self.db.query(WorkflowTask).filter_by(type="packer_pack").count(), 1)

    def test_completed_finished_goods_task_is_reopened_for_next_batch(self):
        first = create_task(
            self.db, order_id=self.order.id, task_type="warehouse_finished_goods",
            title="Оприходовать", role="warehouse",
            payload={
                "product_context": self.context,
                "finished_goods": [{**self.context, "qty": 2}],
                "unit_ids": [101, 102],
                "serial_numbers": ["UTUD-101", "UTUD-102"],
            },
        )
        first.status = "done"
        first.completed_at = utcnow()
        first.payload = {
            **first.payload,
            "completion": {
                "accepted_goods": [{"product_id": self.product.id, "qty": 2}],
            },
        }

        reopened = create_task(
            self.db, order_id=self.order.id, task_type="warehouse_finished_goods",
            title="Оприходовать следующую партию", role="warehouse",
            payload={
                "product_context": self.context,
                "finished_goods": [{**self.context, "qty": 1}],
                "unit_ids": [103],
                "serial_numbers": ["UTUD-103"],
            },
        )

        self.assertEqual(reopened.id, first.id)
        self.assertEqual(reopened.status, "assigned")
        self.assertIsNone(reopened.completed_at)
        self.assertEqual(reopened.payload["completion"], {})
        self.assertEqual(
            reopened.payload["completion_history"][-1]["accepted_goods"],
            [{"product_id": self.product.id, "qty": 2}],
        )
        self.assertEqual(reopened.payload["serial_numbers"], ["UTUD-101", "UTUD-102", "UTUD-103"])
        self.assertEqual(
            self.db.query(WorkflowTask).filter_by(type="warehouse_finished_goods").count(),
            1,
        )

    def test_finished_goods_cannot_be_received_twice(self):
        units = [
            Item(
                order_id=self.order.id,
                product_id=self.product.id,
                serial_number=f"UTUD-{index:03d}",
                status="packed",
            )
            for index in (1, 2)
        ]
        self.db.add_all(units)
        self.db.flush()
        task = create_task(
            self.db,
            order_id=self.order.id,
            task_type="warehouse_finished_goods",
            title="Оприходовать",
            role="warehouse",
            payload={
                "product_context": self.context,
                "finished_goods": [{**self.context, "qty": 2}],
                "unit_ids": [unit.id for unit in units],
                "serial_numbers": [unit.serial_number for unit in units],
            },
        )

        self._take_and_complete(task, {
            "accepted_goods": [{"product_id": self.product.id, "qty": 2}],
        })
        task.status = "assigned"
        task.completed_at = None

        with self.assertRaises(HTTPException) as error:
            self._take_and_complete(task, {
                "accepted_goods": [{"product_id": self.product.id, "qty": 2}],
            })

        self.assertEqual(error.exception.detail, "Нельзя принять 2 шт.: к оприходованию доступно 0 шт.")
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(task_id=task.id, direction="incoming").count(),
            1,
        )
        self.assertEqual(self.db.query(Stock).filter_by(product_id=self.product.id).one().actual_qty, 2)

    def test_other_product_does_not_keep_packing_task_open(self):
        other_product = ProductType(name="БКУ", sku="BKU")
        self.db.add(other_product)
        self.db.flush()
        other_item = OrderItem(order_id=self.order.id, product_id=other_product.id, quantity=1)
        self.db.add(other_item)
        self.db.flush()
        other_context = {
            "order_item_id": other_item.id,
            "product_id": other_product.id,
            "product_name": other_product.name,
            "drawing_number": None,
            "qty": 1,
        }
        unit = Item(
            order_id=self.order.id,
            product_id=self.product.id,
            serial_number="UTUD-001",
            status="passed",
        )
        self.db.add(unit)
        self.db.flush()
        packing = create_task(
            self.db,
            order_id=self.order.id,
            task_type="packer_pack",
            title="Упаковать УТУД",
            role="packer",
            payload={
                "product_context": self.context,
                "product_lines": [{**self.context, "qty": 1}],
                "unit_ids": [unit.id],
                "serial_numbers": [unit.serial_number],
            },
        )
        create_task(
            self.db,
            order_id=self.order.id,
            task_type="tester_check",
            title="Проверить БКУ",
            role="tester",
            payload={
                "product_context": other_context,
                "product_lines": [other_context],
            },
        )

        self._take_and_complete(packing, {"packed_qty": 1})

        self.assertEqual(packing.status, "done")
        self.assertIsNotNone(packing.completed_at)


if __name__ == "__main__":
    unittest.main()
