import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.auth import User
from app.models.inventory import Component, Stock
from app.models.production import (
    Item,
    Order,
    OrderCancellationObligation,
    OrderItem,
    ProductType,
    Reservation,
    WorkflowTask,
)
from app.services.cancellation_service import (
    analyze_order_cancellation,
    approve_order_cancellation,
    resolve_obligation,
    try_finalize_cancellation,
)
from app.services.workflow_service import complete_task, create_initial_order_tasks, create_task


class PreassemblyAndCancellationTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        self.manager = User(
            username="preassembly_manager",
            password_hash="x",
            role="production_manager",
            roles=["production_manager", "warehouse", "tester", "assembler", "repair_engineer"],
        )
        self.product = ProductType(
            name="БКУ",
            sku="BKU-PRETEST",
            drawing_number="TEST.001",
            requires_preassembly_test=True,
            test_checklist=["Проверка питания"],
        )
        self.order = Order(customer_name="Тест предварительной проверки")
        self.db.add_all([self.manager, self.product, self.order])
        self.db.flush()
        self.order_item = OrderItem(
            order_id=self.order.id,
            product_id=self.product.id,
            quantity=2,
        )
        self.db.add(self.order_item)
        self.db.flush()
        self.context = {
            "order_item_id": self.order_item.id,
            "product_id": self.product.id,
            "product_name": self.product.name,
            "drawing_number": self.product.drawing_number,
            "qty": 2,
        }

    def tearDown(self):
        self.db.rollback()
        self.db.close()
        self.engine.dispose()

    def _complete(self, task, payload):
        task.status = "in_progress"
        task.assigned_user_id = self.manager.id
        return complete_task(self.db, task, payload, actor_user_id=self.manager.id)

    def _serial_results(self, task, defective_serials=None, serial_numbers=None):
        defective_serials = set(defective_serials or [])
        return [
            {
                "serial_number": serial_number,
                "reviewed": True,
                "checklist": [{
                    "id": "check-1",
                    "label": "Проверка питания",
                    "checked": serial_number not in defective_serials,
                }],
            }
            for serial_number in (
                task.payload["serial_numbers"]
                if serial_numbers is None
                else serial_numbers
            )
        ]

    def test_material_receipt_creates_preassembly_test_and_holds_assembly(self):
        component = Component(name="Компонент БКУ", part_number="BKU-C1", category="Тест")
        self.db.add(component)
        self.db.flush()
        self.db.add(Stock(component_id=component.id, actual_qty=2, reserved_qty=0, location="A-1"))
        self.db.flush()
        material = {
            "component_id": component.id,
            "line_uid": f"{self.order_item.id}:{component.id}:0",
            "order_item_id": self.order_item.id,
            "product_id": self.product.id,
            "product_name": self.product.name,
            "qty": 2,
            "required_qty": 2,
        }
        create_initial_order_tasks(
            self.db,
            self.order,
            materials=[material],
            shortages=[],
            product_context=self.context,
            available_materials=[material],
        )

        issue = self.db.query(WorkflowTask).filter_by(type="warehouse_issue_materials").one()
        self._complete(issue, {})
        receipt = self.db.query(WorkflowTask).filter_by(type="assembler_receive_materials").one()
        self._complete(receipt, {})

        assembly = self.db.query(WorkflowTask).filter_by(type="assembler_build").one()
        pretest = self.db.query(WorkflowTask).filter_by(type="tester_check").one()
        self.assertEqual(receipt.status, "done")
        self.assertEqual(issue.status, "done")
        self.assertEqual(assembly.status, "hold")
        self.assertTrue(pretest.payload["pre_assembly"])
        self.assertEqual(pretest.payload["source_assembly_task_id"], assembly.id)
        self.assertEqual(len(pretest.payload["unit_ids"]), 2)
        self.assertEqual(
            {unit.status for unit in self.db.query(Item).filter_by(assembly_task_id=assembly.id)},
            {"testing"},
        )
        with self.assertRaisesRegex(HTTPException, "предварительное тестирование"):
            self._complete(assembly, {"assembled_qty": 2})

    def test_preassembly_defect_is_repaired_retested_and_returned_to_same_assembly(self):
        create_initial_order_tasks(
            self.db,
            self.order,
            materials=[],
            shortages=[],
            product_context=self.context,
        )
        assembly = self.db.query(WorkflowTask).filter_by(type="assembler_build").one()
        pretest = self.db.query(WorkflowTask).filter_by(type="tester_check").one()
        defective_serial = pretest.payload["serial_numbers"][0]
        self._complete(pretest, {
            "serial_test_results": self._serial_results(pretest, {defective_serial}),
            "notes": "Не запускается",
        })

        self.assertEqual(
            self.db.query(WorkflowTask).filter_by(type="packer_pack").count(),
            0,
        )
        repair = self.db.query(WorkflowTask).filter_by(type="repair_defects", status="assigned").one()
        self.assertTrue(repair.payload["pre_assembly"])
        self._complete(repair, {
            "serial_repair_results": [{
                "serial_number": defective_serial,
                "work_done": "Заменён стабилизатор питания",
            }],
        })

        retest = self.db.query(WorkflowTask).filter_by(type="tester_check", status="assigned").one()
        self.assertTrue(retest.payload["pre_assembly"])
        self.assertEqual(retest.payload["source_assembly_task_id"], assembly.id)
        self._complete(retest, {
            "serial_test_results": self._serial_results(
                retest,
                serial_numbers=[defective_serial],
            ),
        })

        self.db.refresh(assembly)
        self.assertEqual(assembly.status, "assigned")
        self.assertTrue(assembly.payload["preassembly_test_completed"])
        self.assertEqual(
            set(assembly.payload["preassembly_passed_serial_numbers"]),
            set(assembly.payload["serial_numbers"]),
        )
        self.assertEqual(
            self.db.query(WorkflowTask).filter_by(type="assembler_build").count(),
            1,
        )
        self.assertEqual(
            self.db.query(WorkflowTask).filter_by(type="packer_pack").count(),
            0,
        )

        assembly.payload = {
            **(assembly.payload or {}),
            "assembly_claims": {
                serial_number: self.manager.id
                for serial_number in assembly.payload["serial_numbers"]
            },
        }
        self._complete(assembly, {
            "assembled_serial_numbers": assembly.payload["serial_numbers"],
            "assembled_qty": 2,
        })
        final_test = self.db.query(WorkflowTask).filter_by(type="tester_check", status="assigned").one()
        self.assertFalse(final_test.payload.get("pre_assembly", False))
        self.assertTrue(final_test.title.startswith("Протестировать изделия"))
        other_product = ProductType(name="Другое изделие", sku="OTHER-ACTIVE")
        self.db.add(other_product)
        self.db.flush()
        other_item = OrderItem(order_id=self.order.id, product_id=other_product.id, quantity=1)
        self.db.add(other_item)
        self.db.flush()
        create_task(
            self.db,
            order_id=self.order.id,
            task_type="assembler_build",
            title="Сборка другого изделия",
            role="assembler",
            payload={
                "product_context": {
                    "order_item_id": other_item.id,
                    "product_id": other_product.id,
                    "product_name": other_product.name,
                    "qty": 1,
                },
                "planned_qty": 1,
            },
        )
        final_serial_numbers = [
            unit.serial_number
            for unit in self.db.query(Item).filter_by(
                assembly_task_id=assembly.id,
                status="testing",
            ).all()
        ]
        self.assertEqual(len(final_serial_numbers), 2)
        self._complete(final_test, {
            "serial_test_results": self._serial_results(
                final_test,
                serial_numbers=final_serial_numbers,
            ),
        })
        self.assertEqual(final_test.status, "done")
        self.assertEqual(
            self.db.query(WorkflowTask).filter_by(type="packer_pack", status="assigned").count(),
            1,
        )

    def test_cancellation_releases_reservations_and_stops_active_tasks(self):
        component = Component(name="Резерв отмены", part_number="CANCEL-C1", category="Тест")
        self.db.add(component)
        self.db.flush()
        stock = Stock(component_id=component.id, actual_qty=10, reserved_qty=4, location="A-2")
        reservation = Reservation(order_id=self.order.id, component_id=component.id, qty=4)
        procurement = create_task(
            self.db,
            order_id=self.order.id,
            task_type="procurement_purchase",
            title="Закупить",
            role="procurement",
            payload={"shortages": [{"component_id": component.id, "qty": 2}]},
        )
        self.db.add_all([stock, reservation])
        self.db.flush()

        self.order.cancellation_status = "cancellation_requested"
        self.order.cancellation_reason = "Заказ отменён заказчиком"
        obligations = analyze_order_cancellation(self.db, self.order, self.manager.id)
        approve_order_cancellation(self.db, self.order, self.manager)
        decisions = {
            "release_reservations": "released",
            "procurement_commitments": "cancelled",
            "stop_confirmation": "stopped",
        }
        for obligation in obligations:
            resolve_obligation(
                self.db,
                obligation,
                self.manager,
                {"decision": decisions.get(obligation.obligation_type, "stopped")},
            )
        self.assertTrue(try_finalize_cancellation(self.db, self.order, self.manager.id))

        self.db.refresh(stock)
        self.db.refresh(procurement)
        self.assertEqual(self.db.query(Reservation).filter_by(order_id=self.order.id).count(), 0)
        self.assertEqual(stock.reserved_qty, 0)
        self.assertEqual(procurement.status, "cancelled")
        self.assertEqual(self.order.status, "Cancelled")
        self.assertEqual(self.order.cancellation_status, "cancelled")
        self.assertEqual(
            self.db.query(OrderCancellationObligation).filter_by(order_id=self.order.id).count(),
            len(obligations),
        )


if __name__ == "__main__":
    unittest.main()
