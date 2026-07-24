"""add accumulative workflow batches

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
"""
from alembic import op
import sqlalchemy as sa

revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "workflow_batches" not in existing:
        op.create_table("workflow_batches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
            sa.Column("container_task_id", sa.Integer(), sa.ForeignKey("workflow_tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_task_id", sa.Integer(), sa.ForeignKey("workflow_tasks.id")),
            sa.Column("stage", sa.String(), nullable=False), sa.Column("cycle", sa.String(), nullable=False, server_default="primary"),
            sa.Column("batch_key", sa.String(), nullable=False), sa.Column("status", sa.String(), nullable=False, server_default="queued"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("processed_at", sa.DateTime()),
            sa.UniqueConstraint("container_task_id", "batch_key", name="uq_workflow_batch_container_key"))
        op.create_index("ix_workflow_batches_order_id", "workflow_batches", ["order_id"])
        op.create_index("ix_workflow_batches_container_task_id", "workflow_batches", ["container_task_id"])
        op.create_index("ix_workflow_batches_source_task_id", "workflow_batches", ["source_task_id"])
    if "workflow_batch_lines" not in existing:
        op.create_table("workflow_batch_lines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("workflow_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("entity_type", sa.String(), nullable=False), sa.Column("entity_id", sa.Integer(), nullable=False),
            sa.Column("quantity", sa.Float(), nullable=False), sa.Column("processed_qty", sa.Float(), nullable=False, server_default="0"),
            sa.Column("rejected_qty", sa.Float(), nullable=False, server_default="0"))
        op.create_index("ix_workflow_batch_lines_batch_id", "workflow_batch_lines", ["batch_id"])


def downgrade():
    op.drop_table("workflow_batch_lines")
    op.drop_table("workflow_batches")
