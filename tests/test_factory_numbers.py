import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.auth import User
from app.models.production import Item, Order, OrderItem, ProductType, WorkflowTask
from app.api.tasks import AssemblyAllocationPayload, AssemblyPlanPayload, TestingClaimsPayload, _task_payload, create_assembly_plan, update_assembly_claims, update_testing_claims
from app.services.factory_number_service import create_product_units
from app.services.workflow_batch_service import batch_summary
from app.services.workflow_service import complete_task, create_task


class FactoryNumberTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(username="assembler", password_hash="x", role="assembler")
        self.product = ProductType(name="Устройство тестовое", sku="UTUD")
        self.order = Order(customer_name="Тест")
        self.db.add_all([self.user, self.product, self.order])
        self.db.flush()
        self.order_item = OrderItem(order_id=self.order.id, product_id=self.product.id, quantity=3)
        self.task = WorkflowTask(
            order_id=self.order.id,
            product_id=self.product.id,
            type="assembler_build",
            title="Сборка",
            role="assembler",
            assigned_user_id=self.user.id,
        )
        self.db.add_all([self.order_item, self.task])
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_numbers_are_unique_and_continue_for_same_product(self):
        first = create_product_units(
            self.db,
            order_id=self.order.id,
            order_item_id=self.order_item.id,
            product=self.product,
            assembly_task_id=self.task.id,
            assigned_user_id=self.user.id,
            quantity=2,
        )
        second = create_product_units(
            self.db,
            order_id=self.order.id,
            order_item_id=self.order_item.id,
            product=self.product,
            assembly_task_id=self.task.id,
            assigned_user_id=self.user.id,
            quantity=1,
        )

        numbers = [unit.serial_number for unit in [*first, *second]]
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertEqual(numbers, ["UTUD-001", "UTUD-002", "UTUD-003"])
        self.assertEqual(self.db.query(Item).count(), 3)
        self.assertTrue(all(unit.status == "planned" for unit in [*first, *second]))

    def test_drawing_number_is_used_before_sku(self):
        self.product.drawing_number = "АБВГ.123456.001"
        units = create_product_units(
            self.db,
            order_id=self.order.id,
            order_item_id=self.order_item.id,
            product=self.product,
            assembly_task_id=self.task.id,
            assigned_user_id=self.user.id,
            quantity=2,
        )

        self.assertEqual(
            [unit.serial_number for unit in units],
            ["АБВГ.123456.001-001", "АБВГ.123456.001-002"],
        )

    def test_testing_task_keeps_serial_numbers_from_processed_and_new_batches(self):
        first_units = create_product_units(
            self.db,
            order_id=self.order.id,
            order_item_id=self.order_item.id,
            product=self.product,
            assembly_task_id=self.task.id,
            assigned_user_id=self.user.id,
            quantity=2,
        )
        for unit in first_units:
            unit.status = "testing"
        testing = create_task(
            self.db,
            order_id=self.order.id,
            task_type="tester_check",
            title="Тестирование",
            role="tester",
            payload={
                "product_context": {
                    "order_item_id": self.order_item.id,
                    "product_id": self.product.id,
                },
                "product_lines": [{"product_id": self.product.id, "qty": 2}],
                "unit_ids": [unit.id for unit in first_units],
                "serial_numbers": [unit.serial_number for unit in first_units],
            },
        )

        for unit in first_units:
            unit.status = "passed"
        second_units = create_product_units(
            self.db,
            order_id=self.order.id,
            order_item_id=self.order_item.id,
            product=self.product,
            assembly_task_id=self.task.id,
            assigned_user_id=self.user.id,
            quantity=1,
        )
        second_units[0].status = "testing"
        same_testing = create_task(
            self.db,
            order_id=self.order.id,
            task_type="tester_check",
            title="Тестирование",
            role="tester",
            payload={
                "product_context": {
                    "order_item_id": self.order_item.id,
                    "product_id": self.product.id,
                },
                "product_lines": [{"product_id": self.product.id, "qty": 1}],
                "unit_ids": [second_units[0].id],
                "serial_numbers": [second_units[0].serial_number],
            },
        )

        payload = _task_payload(same_testing, self.db)["payload"]
        all_numbers = [unit.serial_number for unit in [*first_units, *second_units]]
        self.assertEqual(testing.id, same_testing.id)
        self.assertEqual(payload["serial_numbers"], all_numbers)
        self.assertEqual(payload["pending_serial_numbers"], [second_units[0].serial_number])
        self.assertEqual(payload["serial_number_statuses"], {
            first_units[0].serial_number: "passed",
            first_units[1].serial_number: "passed",
            second_units[0].serial_number: "testing",
        })

    def test_each_serial_number_is_routed_by_its_own_checklist(self):
        units = create_product_units(
            self.db,
            order_id=self.order.id,
            order_item_id=self.order_item.id,
            product=self.product,
            assembly_task_id=self.task.id,
            assigned_user_id=self.user.id,
            quantity=3,
        )
        for unit in units:
            unit.status = "testing"
        testing = create_task(
            self.db,
            order_id=self.order.id,
            task_type="tester_check",
            title="Тестирование",
            role="tester",
            payload={
                "product_context": {
                    "order_item_id": self.order_item.id,
                    "product_id": self.product.id,
                    "product_name": self.product.name,
                },
                "product_lines": [{
                    "order_item_id": self.order_item.id,
                    "product_id": self.product.id,
                    "product_name": self.product.name,
                    "qty": 3,
                }],
                "unit_ids": [unit.id for unit in units],
                "serial_numbers": [unit.serial_number for unit in units],
            },
        )
        self.task.status = "done"
        testing.status = "in_progress"
        checklist = [{"id": "power", "label": "Включение"}, {"id": "display", "label": "Экран"}]
        result = complete_task(self.db, testing, {
            "serial_test_results": [
                {
                    "serial_number": units[0].serial_number,
                    "reviewed": True,
                    "checklist": [{**item, "checked": True} for item in checklist],
                },
                {
                    "serial_number": units[1].serial_number,
                    "reviewed": True,
                    "checklist": [
                        {**checklist[0], "checked": True},
                        {**checklist[1], "checked": False},
                    ],
                },
                {
                    "serial_number": units[2].serial_number,
                    "reviewed": True,
                    "checklist": [{**item, "checked": True} for item in checklist],
                },
            ],
        })

        self.assertEqual(result["status"], "done")
        self.assertEqual([unit.status for unit in units], ["passed", "repair", "passed"])
        repair = self.db.query(WorkflowTask).filter_by(type="repair_defects").one()
        packing = self.db.query(WorkflowTask).filter_by(type="packer_pack").one()
        self.assertEqual(repair.payload["serial_numbers"], [units[1].serial_number])
        self.assertEqual(repair.payload["serial_defects"], [{
            "serial_number": units[1].serial_number,
            "product_id": self.product.id,
            "failed_checks": [{"id": "display", "label": "Экран"}],
            "tester_note": "",
        }])
        self.assertEqual(
            packing.payload["serial_numbers"],
            [units[0].serial_number, units[2].serial_number],
        )

        repair.status = "in_progress"
        complete_task(self.db, repair, {
            "serial_repair_results": [{
                "serial_number": units[1].serial_number,
                "work_done": "Заменён дисплейный модуль",
            }],
        })
        retest = self.db.query(WorkflowTask).filter_by(type="tester_check", status="assigned").one()
        self.assertEqual(retest.id, testing.id)
        self.assertEqual(self.db.query(WorkflowTask).filter_by(type="tester_check").count(), 1)
        self.assertNotIn("retest", retest.payload)
        self.assertEqual(retest.payload["retest_serial_numbers"], [units[1].serial_number])
        retest_view = _task_payload(retest, self.db)["payload"]
        self.assertEqual(retest_view["pending_serial_numbers"], [units[1].serial_number])
        self.assertEqual(retest.payload["repair_results"], [{
            "serial_number": units[1].serial_number,
            "work_done": "Заменён дисплейный модуль",
        }])
        self.assertEqual(retest_view["pending_product_lines"][0]["qty"], 1)
        self.assertEqual(batch_summary(self.db, retest)["pending_qty"], 1)

    def test_second_assembly_delivery_does_not_move_first_batch_out_of_testing(self):
        self.task.payload = {
            "product_context": {
                "order_item_id": self.order_item.id,
                "product_id": self.product.id,
                "product_name": self.product.name,
                "qty": 3,
            },
            "product_lines": [{
                "order_item_id": self.order_item.id,
                "product_id": self.product.id,
                "product_name": self.product.name,
                "qty": 3,
            }],
            "materials_complete": True,
        }
        units = create_product_units(
            self.db,
            order_id=self.order.id,
            order_item_id=self.order_item.id,
            product=self.product,
            assembly_task_id=self.task.id,
            assigned_user_id=self.user.id,
            quantity=3,
        )
        self.task.status = "in_progress"

        complete_task(self.db, self.task, {"daily_qty": 2})
        self.assertEqual([unit.status for unit in units], ["testing", "testing", "in_assembly"])

        self.task.status = "in_progress"
        complete_task(self.db, self.task, {"daily_qty": 1})
        self.assertEqual([unit.status for unit in units], ["testing", "testing", "testing"])

    def test_assembler_selects_exact_serial_number_for_testing(self):
        self.task.payload = {
            "product_context": {
                "order_item_id": self.order_item.id,
                "product_id": self.product.id,
                "product_name": self.product.name,
                "qty": 3,
            },
            "product_lines": [{
                "order_item_id": self.order_item.id,
                "product_id": self.product.id,
                "product_name": self.product.name,
                "qty": 3,
            }],
            "materials_complete": True,
        }
        units = create_product_units(
            self.db,
            order_id=self.order.id,
            order_item_id=self.order_item.id,
            product=self.product,
            assembly_task_id=self.task.id,
            assigned_user_id=self.user.id,
            quantity=3,
        )
        selected_serial = units[2].serial_number
        self.task.status = "in_progress"

        saved = complete_task(self.db, self.task, {
            "assembled_serial_numbers": [selected_serial],
            "save_only": True,
        })
        self.assertEqual(saved["status"], "partial")
        self.assertEqual([unit.status for unit in units], ["in_assembly", "in_assembly", "assembled"])

        self.task.status = "in_progress"
        complete_task(self.db, self.task, {
            "assembled_serial_numbers": [selected_serial],
        })
        testing = self.db.query(WorkflowTask).filter_by(type="tester_check").one()
        self.assertEqual(testing.payload["serial_numbers"], [selected_serial])
        self.assertEqual([unit.status for unit in units], ["in_assembly", "in_assembly", "testing"])

    def test_reviewed_serials_can_leave_testing_before_whole_batch_is_checked(self):
        units = create_product_units(
            self.db,
            order_id=self.order.id,
            order_item_id=self.order_item.id,
            product=self.product,
            assembly_task_id=self.task.id,
            assigned_user_id=self.user.id,
            quantity=3,
        )
        for unit in units:
            unit.status = "testing"
        testing = create_task(
            self.db,
            order_id=self.order.id,
            task_type="tester_check",
            title="Тестирование",
            role="tester",
            payload={
                "product_context": {
                    "order_item_id": self.order_item.id,
                    "product_id": self.product.id,
                    "product_name": self.product.name,
                },
                "product_lines": [{
                    "order_item_id": self.order_item.id,
                    "product_id": self.product.id,
                    "product_name": self.product.name,
                    "qty": 3,
                }],
                "unit_ids": [unit.id for unit in units],
                "serial_numbers": [unit.serial_number for unit in units],
            },
        )
        self.task.status = "done"
        testing.status = "in_progress"

        result = complete_task(self.db, testing, {
            "serial_test_results": [
                {
                    "serial_number": units[0].serial_number,
                    "reviewed": True,
                    "checklist": [{"id": "power", "label": "Включение", "checked": True}],
                },
                {
                    "serial_number": units[1].serial_number,
                    "reviewed": False,
                    "checklist": [{"id": "power", "label": "Включение", "checked": False}],
                },
                {
                    "serial_number": units[2].serial_number,
                    "reviewed": False,
                    "checklist": [{"id": "power", "label": "Включение", "checked": False}],
                },
            ],
        })

        self.assertEqual(result["status"], "partial")
        self.assertEqual([unit.status for unit in units], ["passed", "testing", "testing"])
        self.assertEqual(testing.status, "assigned")
        packing = self.db.query(WorkflowTask).filter_by(type="packer_pack").one()
        self.assertEqual(packing.payload["serial_numbers"], [units[0].serial_number])

    def test_packer_routes_exact_selected_serial_number_to_finished_goods(self):
        self.product.drawing_number = "АБВГ.123456.001"
        units = create_product_units(
            self.db,
            order_id=self.order.id,
            order_item_id=self.order_item.id,
            product=self.product,
            assembly_task_id=self.task.id,
            assigned_user_id=self.user.id,
            quantity=3,
        )
        for unit in units:
            unit.status = "passed"
        packing = create_task(
            self.db,
            order_id=self.order.id,
            task_type="packer_pack",
            title="Упаковка",
            role="packer",
            payload={
                "product_context": {
                    "order_item_id": self.order_item.id,
                    "product_id": self.product.id,
                    "product_name": self.product.name,
                    "drawing_number": self.product.drawing_number,
                },
                "product_lines": [{
                    "order_item_id": self.order_item.id,
                    "product_id": self.product.id,
                    "product_name": self.product.name,
                    "drawing_number": self.product.drawing_number,
                    "qty": 3,
                }],
                "unit_ids": [unit.id for unit in units],
                "serial_numbers": [unit.serial_number for unit in units],
            },
        )
        packing.status = "in_progress"
        packing_view = _task_payload(packing, self.db)["payload"]

        self.assertEqual(
            packing_view["serial_units"][0]["drawing_number"],
            self.product.drawing_number,
        )
        complete_task(self.db, packing, {
            "packed_serial_numbers": [units[2].serial_number],
        })

        finished_goods = self.db.query(WorkflowTask).filter_by(type="warehouse_finished_goods").one()
        self.assertEqual(finished_goods.payload["serial_numbers"], [units[2].serial_number])
        self.assertEqual(units[2].status, "packed")
        self.assertEqual([units[0].status, units[1].status], ["passed", "passed"])

    def test_packer_can_select_units_from_all_waiting_batches(self):
        units = create_product_units(
            self.db,
            order_id=self.order.id,
            order_item_id=self.order_item.id,
            product=self.product,
            assembly_task_id=self.task.id,
            assigned_user_id=self.user.id,
            quantity=6,
        )
        for unit in units:
            unit.status = "passed"
        payload_base = {
            "product_context": {
                "order_item_id": self.order_item.id,
                "product_id": self.product.id,
                "product_name": self.product.name,
            },
        }
        packing = create_task(
            self.db,
            order_id=self.order.id,
            task_type="packer_pack",
            title="Упаковка",
            role="packer",
            payload={
                **payload_base,
                "product_lines": [{"product_id": self.product.id, "product_name": self.product.name, "qty": 4}],
                "unit_ids": [unit.id for unit in units[:4]],
                "serial_numbers": [unit.serial_number for unit in units[:4]],
            },
        )
        same_packing = create_task(
            self.db,
            order_id=self.order.id,
            task_type="packer_pack",
            title="Упаковка",
            role="packer",
            payload={
                **payload_base,
                "product_lines": [{"product_id": self.product.id, "product_name": self.product.name, "qty": 2}],
                "unit_ids": [unit.id for unit in units[4:]],
                "serial_numbers": [unit.serial_number for unit in units[4:]],
            },
        )
        self.assertEqual(same_packing.id, packing.id)
        self.assertEqual(
            _task_payload(packing, self.db)["payload"]["pending_product_lines"][0]["qty"],
            6,
        )
        packing.status = "in_progress"

        complete_task(self.db, packing, {
            "packed_serial_numbers": [unit.serial_number for unit in units],
        })

        finished_goods = self.db.query(WorkflowTask).filter_by(type="warehouse_finished_goods").one()
        self.assertEqual(finished_goods.payload["serial_numbers"], [unit.serial_number for unit in units])
        self.assertTrue(all(unit.status == "packed" for unit in units))

    def test_two_testers_claim_different_serial_numbers_without_conflict(self):
        first_tester = User(username="tester-1", password_hash="x", role="tester")
        second_tester = User(username="tester-2", password_hash="x", role="tester")
        self.db.add_all([first_tester, second_tester])
        self.db.flush()
        units = create_product_units(
            self.db,
            order_id=self.order.id,
            order_item_id=self.order_item.id,
            product=self.product,
            assembly_task_id=self.task.id,
            assigned_user_id=self.user.id,
            quantity=2,
        )
        for unit in units:
            unit.status = "testing"
        testing = create_task(
            self.db,
            order_id=self.order.id,
            task_type="tester_check",
            title="Совместное тестирование",
            role="tester",
            payload={
                "product_context": {
                    "order_item_id": self.order_item.id,
                    "product_id": self.product.id,
                    "product_name": self.product.name,
                },
                "product_lines": [{"product_id": self.product.id, "product_name": self.product.name, "qty": 2}],
                "unit_ids": [unit.id for unit in units],
                "serial_numbers": [unit.serial_number for unit in units],
            },
        )

        update_testing_claims(
            testing.id,
            TestingClaimsPayload(serial_numbers=[units[0].serial_number]),
            self.db,
            first_tester,
        )
        with self.assertRaises(HTTPException) as conflict:
            update_testing_claims(
                testing.id,
                TestingClaimsPayload(serial_numbers=[units[0].serial_number]),
                self.db,
                second_tester,
            )
        self.assertEqual(conflict.exception.status_code, 409)
        update_testing_claims(
            testing.id,
            TestingClaimsPayload(serial_numbers=[units[1].serial_number]),
            self.db,
            second_tester,
        )

        complete_task(self.db, testing, {
            "serial_test_results": [{
                "serial_number": units[0].serial_number,
                "reviewed": True,
                "checklist": [{"id": "power", "label": "Включение", "checked": True}],
            }],
        }, actor_user_id=first_tester.id)
        self.assertEqual((testing.payload or {}).get("testing_claims"), {
            units[1].serial_number: second_tester.id,
        })
        complete_task(self.db, testing, {
            "serial_test_results": [{
                "serial_number": units[1].serial_number,
                "reviewed": True,
                "checklist": [{"id": "power", "label": "Включение", "checked": True}],
            }],
        }, actor_user_id=second_tester.id)

        self.assertEqual([unit.status for unit in units], ["passed", "passed"])
        self.assertEqual((testing.payload or {}).get("testing_claims"), {})

    def test_two_assemblers_cannot_claim_the_same_serial_number(self):
        second_assembler = User(username="assembler-2", password_hash="x", role="assembler")
        self.db.add(second_assembler)
        self.db.flush()
        self.task.payload = {
            "product_context": {
                "order_item_id": self.order_item.id,
                "product_id": self.product.id,
                "product_name": self.product.name,
                "qty": 1,
            },
            "product_lines": [{
                "order_item_id": self.order_item.id,
                "product_id": self.product.id,
                "product_name": self.product.name,
                "qty": 1,
            }],
            "materials_complete": True,
        }
        unit = create_product_units(
            self.db,
            order_id=self.order.id,
            order_item_id=self.order_item.id,
            product=self.product,
            assembly_task_id=self.task.id,
            assigned_user_id=self.user.id,
            quantity=1,
        )[0]
        self.task.payload = {
            **self.task.payload,
            "unit_ids": [unit.id],
            "serial_numbers": [unit.serial_number],
        }

        with self.assertRaises(HTTPException) as conflict:
            update_assembly_claims(
                self.task.id,
                TestingClaimsPayload(serial_numbers=[unit.serial_number]),
                self.db,
                second_assembler,
            )
        self.assertEqual(conflict.exception.status_code, 409)
        update_assembly_claims(
            self.task.id,
            TestingClaimsPayload(serial_numbers=[unit.serial_number], action="release"),
            self.db,
            self.user,
        )
        update_assembly_claims(
            self.task.id,
            TestingClaimsPayload(serial_numbers=[unit.serial_number]),
            self.db,
            second_assembler,
        )

        complete_task(self.db, self.task, {
            "assembled_serial_numbers": [unit.serial_number],
            "save_only": True,
        }, actor_user_id=second_assembler.id)
        self.assertEqual(unit.status, "assembled")
        self.assertEqual(unit.assigned_user_id, second_assembler.id)

    def test_assembly_plan_creates_one_shared_queue_with_initial_claims(self):
        manager = User(username="manager", password_hash="x", role="production_manager")
        second_assembler = User(username="assembler-2", password_hash="x", role="assembler")
        self.db.add_all([manager, second_assembler])
        self.db.flush()
        self.task.status = "assigned"
        self.task.payload = {
            "product_context": {
                "order_item_id": self.order_item.id,
                "product_id": self.product.id,
                "product_name": self.product.name,
                "qty": 3,
            },
            "product_lines": [{
                "order_item_id": self.order_item.id,
                "product_id": self.product.id,
                "product_name": self.product.name,
                "qty": 3,
            }],
            "materials_complete": True,
        }

        result = create_assembly_plan(
            self.task.id,
            AssemblyPlanPayload(allocations=[
                AssemblyAllocationPayload(
                    user_id=self.user.id,
                    quantity=2,
                    order_item_id=self.order_item.id,
                    product_id=self.product.id,
                ),
                AssemblyAllocationPayload(
                    user_id=second_assembler.id,
                    quantity=1,
                    order_item_id=self.order_item.id,
                    product_id=self.product.id,
                ),
            ]),
            self.db,
            manager,
        )

        self.assertEqual(len(result["tasks"]), 1)
        shared_task = self.db.query(WorkflowTask).filter_by(type="assembler_build").one()
        self.assertIsNone(shared_task.assigned_user_id)
        claim_counts = {}
        for user_id in shared_task.payload["assembly_claims"].values():
            claim_counts[user_id] = claim_counts.get(user_id, 0) + 1
        self.assertEqual(claim_counts, {self.user.id: 2, second_assembler.id: 1})


if __name__ == "__main__":
    unittest.main()
