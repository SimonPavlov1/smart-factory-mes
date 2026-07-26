"""task management core, audit, dependencies and cancellation

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
"""
from alembic import op
import sqlalchemy as sa


revision = "f7a8b9c0d1e2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade():
    task_columns = _columns("workflow_tasks")
    additions = [
        # SQLite cannot add FK constraints with ALTER TABLE. API validation keeps these
        # nullable references consistent; fresh tables below retain database FKs.
        ("product_id", sa.Column("product_id", sa.Integer(), nullable=True)),
        ("created_by_user_id", sa.Column("created_by_user_id", sa.Integer(), nullable=True)),
        ("is_manual", sa.Column("is_manual", sa.Boolean(), nullable=False, server_default=sa.false())),
        ("priority", sa.Column("priority", sa.String(), nullable=False, server_default="normal")),
        ("planned_start_at", sa.Column("planned_start_at", sa.DateTime(), nullable=True)),
        ("estimated_minutes", sa.Column("estimated_minutes", sa.Integer(), nullable=True)),
        ("actual_minutes", sa.Column("actual_minutes", sa.Integer(), nullable=True)),
        ("sla_due_at", sa.Column("sla_due_at", sa.DateTime(), nullable=True)),
        ("deadline_change_reason", sa.Column("deadline_change_reason", sa.Text(), nullable=True)),
        ("hold_reason", sa.Column("hold_reason", sa.Text(), nullable=True)),
        ("cancel_reason", sa.Column("cancel_reason", sa.Text(), nullable=True)),
        ("cancelled_at", sa.Column("cancelled_at", sa.DateTime(), nullable=True)),
        ("updated_at", sa.Column("updated_at", sa.DateTime(), nullable=True)),
    ]
    for name, column in additions:
        if name not in task_columns:
            op.add_column("workflow_tasks", column)

    for name in ["product_id", "created_by_user_id", "is_manual", "priority", "sla_due_at"]:
        op.create_index(f"ix_workflow_tasks_{name}", "workflow_tasks", [name], if_not_exists=True)

    order_columns = _columns("orders")
    order_additions = [
        ("cancellation_status", sa.Column("cancellation_status", sa.String(), nullable=True)),
        ("cancellation_reason", sa.Column("cancellation_reason", sa.Text(), nullable=True)),
        ("cancellation_requested_at", sa.Column("cancellation_requested_at", sa.DateTime(), nullable=True)),
        ("cancellation_requested_by", sa.Column("cancellation_requested_by", sa.Integer(), nullable=True)),
        ("cancellation_approved_at", sa.Column("cancellation_approved_at", sa.DateTime(), nullable=True)),
        ("cancellation_approved_by", sa.Column("cancellation_approved_by", sa.Integer(), nullable=True)),
        ("cancelled_at", sa.Column("cancelled_at", sa.DateTime(), nullable=True)),
        ("financial_impact", sa.Column("financial_impact", sa.Float(), nullable=False, server_default="0")),
        ("cancellation_summary", sa.Column("cancellation_summary", sa.JSON(), nullable=True)),
    ]
    for name, column in order_additions:
        if name not in order_columns:
            op.add_column("orders", column)
    op.create_index("ix_orders_cancellation_status", "orders", ["cancellation_status"], if_not_exists=True)

    # The application intentionally calls metadata.create_all() for local SQLite
    # installations. In that mode new tables may already exist before Alembic is run.
    required_tables = {
        "task_events", "task_watchers", "task_dependencies",
        "task_notifications", "order_cancellation_obligations",
    }
    if required_tables.issubset(set(sa.inspect(op.get_bind()).get_table_names())):
        return

    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("workflow_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("from_status", sa.String(), nullable=True),
        sa.Column("to_status", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    for name in ["task_id", "actor_user_id", "event_type", "created_at"]:
        op.create_index(f"ix_task_events_{name}", "task_events", [name])

    op.create_table(
        "task_watchers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("workflow_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("added_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("task_id", "user_id", name="uq_task_watcher"),
    )
    op.create_index("ix_task_watchers_task_id", "task_watchers", ["task_id"])
    op.create_index("ix_task_watchers_user_id", "task_watchers", ["user_id"])

    op.create_table(
        "task_dependencies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("workflow_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("depends_on_task_id", sa.Integer(), sa.ForeignKey("workflow_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dependency_type", sa.String(), nullable=False, server_default="blocks"),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("task_id", "depends_on_task_id", "dependency_type", name="uq_task_dependency"),
    )
    for name in ["task_id", "depends_on_task_id", "dependency_type"]:
        op.create_index(f"ix_task_dependencies_{name}", "task_dependencies", [name])

    op.create_table(
        "task_notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("task_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("workflow_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("event_id", "user_id", name="uq_task_notification_event_user"),
    )
    for name in ["event_id", "task_id", "user_id"]:
        op.create_index(f"ix_task_notifications_{name}", "task_notifications", [name])

    op.create_table(
        "order_cancellation_obligations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("obligation_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("responsible_role", sa.String(), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("workflow_tasks.id"), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("resolution", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    for name in ["order_id", "obligation_type", "status", "responsible_role", "task_id"]:
        op.create_index(f"ix_order_cancellation_obligations_{name}", "order_cancellation_obligations", [name])


def downgrade():
    op.drop_table("order_cancellation_obligations")
    op.drop_table("task_notifications")
    op.drop_table("task_dependencies")
    op.drop_table("task_watchers")
    op.drop_table("task_events")
    for name in [
        "cancellation_summary", "financial_impact", "cancelled_at",
        "cancellation_approved_by", "cancellation_approved_at",
        "cancellation_requested_by", "cancellation_requested_at",
        "cancellation_reason", "cancellation_status",
    ]:
        op.drop_column("orders", name)
    for name in [
        "updated_at", "cancelled_at", "cancel_reason", "hold_reason",
        "deadline_change_reason", "sla_due_at", "actual_minutes",
        "estimated_minutes", "planned_start_at", "priority", "is_manual",
        "created_by_user_id", "product_id",
    ]:
        op.drop_column("workflow_tasks", name)
