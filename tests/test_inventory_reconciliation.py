import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.inventory import add_stock, update_stock_quantity
from app.database import Base
from app.models.auth import User
from app.models.inventory import Component, Stock
from app.models.production import Order, WorkflowTask
from app.services.workflow_service import create_task


class InventoryReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        self.user = User(
            username="warehouse-test",
            password_hash="test",
            role="warehouse",
        )
        self.component = Component(name="Тестовый компонент", part_number="TEST-STOCK-1")
        self.order = Order(customer_name="Проверка ручного прихода")
        self.db.add_all([self.user, self.component, self.order])
        self.db.flush()
        self.context = {
            "order_item_id": 101,
            "product_id": 202,
            "product_name": "Тестовое изделие",
            "product_qty": 1,
        }
        self.procurement = create_task(
            self.db,
            order_id=self.order.id,
            task_type="procurement_purchase",
            title="Закупить компоненты",
            role="procurement",
            payload={
                "shortages": [{
                    **self.context,
                    "component_id": self.component.id,
                    "qty": 10,
                    "required_qty": 10,
                    "shortage_qty": 10,
                }],
            },
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _issue_tasks(self):
        return self.db.query(WorkflowTask).filter_by(
            order_id=self.order.id,
            type="warehouse_issue_materials",
        ).order_by(WorkflowTask.id).all()

    def test_manual_incoming_reduces_procurement_and_creates_issue_task(self):
        add_stock(
            component_id=self.component.id,
            quantity=4,
            location="A-01",
            db=self.db,
            user=self.user,
        )

        self.db.refresh(self.procurement)
        self.assertEqual(self.procurement.status, "assigned")
        self.assertEqual(self.procurement.payload["shortages"][0]["qty"], 6)
        self.assertEqual(self.procurement.payload["shortages"][0]["shortage_qty"], 6)
        issue = self._issue_tasks()[0]
        self.assertEqual(issue.payload["materials"][0]["qty"], 4)
        stock = self.db.query(Stock).filter_by(component_id=self.component.id).one()
        self.assertEqual(stock.actual_qty, 4)
        self.assertEqual(stock.reserved_qty, 4)

        add_stock(
            component_id=self.component.id,
            quantity=6,
            location="A-01",
            db=self.db,
            user=self.user,
        )

        self.db.refresh(self.procurement)
        self.assertEqual(self.procurement.status, "cancelled")
        self.assertEqual(self.procurement.payload["shortages"], [])
        self.assertEqual(
            [task.payload["materials"][0]["qty"] for task in self._issue_tasks()],
            [4, 6],
        )

    def test_positive_quantity_correction_reconciles_procurement(self):
        update_stock_quantity(
            component_id=self.component.id,
            new_quantity=10,
            db=self.db,
            user=self.user,
        )

        self.db.refresh(self.procurement)
        self.assertEqual(self.procurement.status, "cancelled")
        self.assertEqual(self._issue_tasks()[0].payload["materials"][0]["qty"], 10)


if __name__ == "__main__":
    unittest.main()
