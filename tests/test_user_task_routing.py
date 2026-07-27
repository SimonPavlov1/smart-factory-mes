import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.auth import create_user, update_user
from app.api.tasks import (
    TaskDeadlinePayload,
    _assignee_or_404,
    get_all_tasks,
    get_my_tasks,
    set_task_deadline,
    take_task,
)
from app.database import Base
from app.models.auth import User
from app.models.production import WorkflowTask
from app.schemas.auth import UserCreate, UserUpdate
from app.services.auth_service import user_task_roles


class UserTaskRoutingTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        self.admin = User(
            username="admin",
            password_hash="x",
            role="admin",
            roles=["admin"],
            task_roles=[],
            is_active=True,
        )
        self.manager = User(
            username="manager",
            password_hash="x",
            role="manager",
            roles=["manager"],
            task_roles=["warehouse"],
            is_active=True,
        )
        self.worker = User(
            username="worker",
            password_hash="x",
            role="assembler",
            roles=["assembler"],
            task_roles=["assembler", "accounting"],
            auto_tasks_enabled=True,
            manual_assignment_enabled=True,
            is_active=True,
        )
        self.db.add_all([self.admin, self.manager, self.worker])
        self.db.flush()

    def tearDown(self):
        self.db.rollback()
        self.db.close()
        self.engine.dispose()

    def test_task_queues_are_independent_from_access_roles(self):
        self.assertEqual(user_task_roles(self.worker), ["assembler", "accounting"])
        assignee = _assignee_or_404(self.db, self.worker.id, "accounting")
        self.assertEqual(assignee.id, self.worker.id)

    def test_manual_assignment_can_be_disabled(self):
        self.worker.manual_assignment_enabled = False
        with self.assertRaises(HTTPException) as error:
            _assignee_or_404(self.db, self.worker.id, "assembler")
        self.assertEqual(error.exception.status_code, 422)

    def test_cross_role_automatic_task_is_visible_in_personal_queue(self):
        task = WorkflowTask(
            type="accounting_payment",
            title="Оплатить счёт",
            role="accounting",
            status="assigned",
            payload={},
        )
        self.db.add(task)
        self.db.commit()

        result = get_my_tasks(status="active", db=self.db, user=self.worker)

        self.assertIn(task.id, [item["id"] for item in result])

    def test_manager_without_queues_only_gets_task_after_taking_it(self):
        self.manager.task_roles = []
        task = WorkflowTask(
            type="accounting_payment",
            title="Оплатить счёт",
            role="accounting",
            status="assigned",
            payload={},
        )
        self.db.add(task)
        self.db.commit()

        personal = get_my_tasks(status="active", db=self.db, user=self.manager)
        overview = get_all_tasks(status="active", db=self.db, _=self.manager)

        self.assertNotIn(task.id, [item["id"] for item in personal])
        self.assertIn(task.id, [item["id"] for item in overview])

        take_task(task.id, db=self.db, user=self.manager)
        personal_after_take = get_my_tasks(status="active", db=self.db, user=self.manager)
        self.assertIn(task.id, [item["id"] for item in personal_after_take])

    def test_manager_can_set_deadline_for_task_in_progress(self):
        task = WorkflowTask(
            type="accounting_payment",
            title="Оплатить счёт",
            role="accounting",
            status="in_progress",
            assigned_user_id=self.worker.id,
            payload={},
        )
        self.db.add(task)
        self.db.commit()

        result = set_task_deadline(
            task.id,
            TaskDeadlinePayload(
                due_date="2026-08-10T12:00:00",
                reason="Согласовано с производством",
            ),
            db=self.db,
            actor=self.manager,
        )

        self.assertEqual(result["due_date"].isoformat(), "2026-08-10T12:00:00")

    def test_unassigned_tasks_are_visible_only_for_configured_queue_matrix(self):
        task_roles = [
            "warehouse",
            "manager",
            "engineer",
            "procurement",
            "accounting",
            "assembler",
            "tester",
            "repair_engineer",
            "packer",
            "production",
            "production_manager",
        ]
        users = {}
        tasks = {}
        for index, role in enumerate(task_roles, start=1):
            user = User(
                username=f"queue-{role}",
                password_hash="x",
                role="engineer",
                roles=["engineer"],
                task_roles=[role],
                auto_tasks_enabled=True,
                manual_assignment_enabled=True,
                is_active=True,
            )
            task = WorkflowTask(
                type="manual",
                title=f"Задача очереди {role}",
                role=role,
                status="assigned",
                is_manual=True,
                priority="normal",
                sort_order=index,
                payload={},
            )
            self.db.add_all([user, task])
            users[role] = user
            tasks[role] = task
        self.db.commit()

        for role in task_roles:
            visible = get_my_tasks(status="active", db=self.db, user=users[role])
            matrix_task_ids = {
                item["id"] for item in visible if item["id"] in {task.id for task in tasks.values()}
            }
            self.assertEqual(
                matrix_task_ids,
                {tasks[role].id},
                f"Сотрудник очереди {role} получил задачи чужих очередей",
            )

    def test_disabled_automatic_queue_hides_unassigned_task(self):
        self.worker.auto_tasks_enabled = False
        task = WorkflowTask(
            type="manual",
            title="Автоматическая задача сборщика",
            role="assembler",
            status="assigned",
            is_manual=True,
            priority="normal",
            payload={},
        )
        self.db.add(task)
        self.db.commit()

        result = get_my_tasks(status="active", db=self.db, user=self.worker)

        self.assertNotIn(task.id, [item["id"] for item in result])

    def test_manager_cannot_create_or_edit_admin(self):
        with self.assertRaises(HTTPException) as create_error:
            create_user(
                UserCreate(
                    password="secret1",
                    phone="+79000000001",
                    role="admin",
                    roles=["admin"],
                ),
                self.db,
                self.manager,
            )
        self.assertEqual(create_error.exception.status_code, 403)

        with self.assertRaises(HTTPException) as update_error:
            update_user(
                self.admin.id,
                UserUpdate(first_name="Изменено"),
                self.db,
                self.manager,
            )
        self.assertEqual(update_error.exception.status_code, 403)
