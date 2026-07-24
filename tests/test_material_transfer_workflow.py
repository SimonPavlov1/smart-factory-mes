import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.auth import User  # noqa: F401
from app.models.inventory import Component, InventoryMovement, Stock  # noqa: F401
from app.models.procurement import PurchaseItem, PurchaseOrder  # noqa: F401
from app.models.production import MaterialTransfer, Order, WorkflowTask
from app.services.workflow_service import (
    _accept_material_transfer,
    _attach_material_transfer_receipt,
    _mark_material_transfer_issued,
    complete_task,
    create_task,
)


class MaterialTransferWorkflowTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine, autoflush=False)()
        self.order = Order(customer_name="Тест", status="In Assembly")
        self.db.add(self.order)
        self.db.flush()

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def _issue_task(self, source_type: str, recipient_role: str):
        task = create_task(
            self.db,
            order_id=self.order.id,
            task_type="repair_issue_materials",
            title="Выдать компоненты",
            role="warehouse",
            payload={
                "source_task_id": 100,
                "source_task_type": source_type,
                "counterparty_role": recipient_role,
                "materials": [{
                    "component_id": 1,
                    "line_uid": "line-1",
                    "qty": 2,
                    "requested_qty": 2,
                }],
            },
        )
        task.status = "in_progress"
        return task

    def test_transfer_quantities_move_from_reserved_to_accepted(self):
        issue = self._issue_task("assembler_build", "assembler")
        transfer = _mark_material_transfer_issued(self.db, issue, "assembler", actor_user_id=1)
        receipt = create_task(
            self.db,
            order_id=self.order.id,
            task_type="assembler_receive_materials",
            title="Получить компоненты",
            role="assembler",
            payload={"source_issue_task_id": issue.id, "materials": issue.payload["materials"]},
        )
        _attach_material_transfer_receipt(self.db, transfer, receipt)
        _accept_material_transfer(self.db, receipt, actor_user_id=2)

        self.assertEqual(transfer.status, "accepted")
        self.assertEqual(transfer.recipient_role, "assembler")
        self.assertEqual(transfer.lines[0].requested_qty, 2)
        self.assertEqual(transfer.lines[0].reserved_qty, 2)
        self.assertEqual(transfer.lines[0].issued_qty, 2)
        self.assertEqual(transfer.lines[0].accepted_qty, 2)

    def test_issue_routes_receipt_by_source_task_type(self):
        cases = [
            ("assembler_build", "assembler", "assembler_receive_materials"),
            ("repair_defects", "repair_engineer", "repair_receive_materials"),
        ]
        with patch("app.services.workflow_service._issue_reserved_materials"):
            for source_type, recipient_role, expected_receipt_type in cases:
                issue = self._issue_task(source_type, recipient_role)
                result = complete_task(self.db, issue, {}, actor_user_id=1)
                receipt = self.db.get(WorkflowTask, result["receive_task_id"])
                self.assertEqual(receipt.type, expected_receipt_type)
                self.assertEqual(receipt.role, recipient_role)
                transfer = self.db.query(MaterialTransfer).filter_by(issue_task_id=issue.id).one()
                self.assertEqual(transfer.status, "issued")


if __name__ == "__main__":
    unittest.main()
