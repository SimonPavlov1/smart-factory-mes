import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.auth import User
from app.models.production import TaskDependency, TaskEvent, TaskNotification, TaskWatcher, WorkflowTask
from app.services.task_management_service import (
    add_mentioned_watchers,
    ensure_dependency_is_valid,
    record_event,
    transition_task,
)


class TaskManagementTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        self.manager = User(username="manager", password_hash="x", role="manager", is_active=True)
        self.worker = User(username="worker", password_hash="x", role="assembler", is_active=True)
        self.watcher = User(username="observer", password_hash="x", role="engineer", is_active=True)
        self.db.add_all([self.manager, self.worker, self.watcher])
        self.db.flush()

    def tearDown(self):
        self.db.rollback()
        self.db.close()
        self.engine.dispose()

    def _task(self, title, status="assigned"):
        task = WorkflowTask(
            type="manual",
            title=title,
            role="assembler",
            status=status,
            is_manual=True,
            priority="normal",
            assigned_user_id=self.worker.id,
            created_by_user_id=self.manager.id,
            payload={},
        )
        self.db.add(task)
        self.db.flush()
        return task

    def test_hold_and_return_require_reason(self):
        task = self._task("Проверить узел", "in_progress")
        with self.assertRaises(HTTPException) as error:
            transition_task(self.db, task, "hold", self.worker)
        self.assertEqual(error.exception.status_code, 422)

        transition_task(self.db, task, "hold", self.worker, reason="Ждём измерительный стенд")
        self.assertEqual(task.status, "hold")
        with self.assertRaises(HTTPException):
            transition_task(self.db, task, "in_progress", self.worker)

    def test_blocker_prevents_start_until_done(self):
        blocker = self._task("Подготовить детали", "in_progress")
        task = self._task("Собрать изделие", "assigned")
        self.db.add(TaskDependency(task_id=task.id, depends_on_task_id=blocker.id, dependency_type="blocks"))
        self.db.flush()

        with self.assertRaises(HTTPException) as error:
            transition_task(self.db, task, "in_progress", self.worker)
        self.assertEqual(error.exception.status_code, 409)

        transition_task(self.db, blocker, "done", self.worker)
        transition_task(self.db, task, "in_progress", self.worker)
        self.assertEqual(task.status, "in_progress")

    def test_mentions_add_watcher_and_event_notifies_them(self):
        task = self._task("Согласовать изменение")
        added = add_mentioned_watchers(self.db, task, "Нужно мнение @observer", self.manager.id)
        self.assertEqual(added, [self.watcher.id])
        self.assertEqual(self.db.query(TaskWatcher).filter_by(task_id=task.id).count(), 1)

        record_event(self.db, task, "comment_added", self.manager.id, data={"text": "готово"})
        self.assertGreaterEqual(
            self.db.query(TaskNotification).filter_by(task_id=task.id, user_id=self.watcher.id).count(),
            1,
        )
        self.assertGreaterEqual(self.db.query(TaskEvent).filter_by(task_id=task.id).count(), 2)

    def test_blocking_dependency_cycle_is_rejected(self):
        first = self._task("Первая")
        second = self._task("Вторая")
        self.db.add(TaskDependency(task_id=first.id, depends_on_task_id=second.id, dependency_type="blocks"))
        self.db.flush()
        with self.assertRaises(HTTPException) as error:
            ensure_dependency_is_valid(self.db, second.id, first.id, "blocks")
        self.assertEqual(error.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
