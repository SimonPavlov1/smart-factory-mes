import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.auth import User  # noqa: F401
from app.models.inventory import InventoryMovement, Stock  # noqa: F401
from app.models.procurement import PurchaseItem, PurchaseOrder  # noqa: F401
from app.models.production import (
    MaterialBatch, Order, OrderItem, ProductType, TaskQuantity, WorkflowBatch, WorkflowTask,
)
from app.services.order_progress_service import aggregate_order_progress
from app.services.workflow_service import complete_task, create_task
from app.api.tasks import TaskCompletePayload, complete_workflow_task
from app.services.workflow_batch_service import batch_summary, pending_product_lines


class FullOrderCycleTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine, autoflush=False)()
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
        self.assertTrue(retest.payload["retest"])
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

        self._take_and_complete(assembly, {"assembled_qty": 10})
        testing_tasks = self.db.query(WorkflowTask).filter_by(
            order_id=self.order.id, type="tester_check",
        ).all()
        self.assertEqual(len(testing_tasks), 1)
        self.assertEqual(batch_summary(self.db, testing)["batches_total"], 2)
        self.assertEqual(batch_summary(self.db, testing)["pending_qty"], 7)

    def test_testing_form_shows_one_delivery_not_cumulative_history(self):
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
        self.assertEqual(sum(line["qty"] for line in pending_product_lines(self.db, first)), 3)


if __name__ == "__main__":
    unittest.main()
