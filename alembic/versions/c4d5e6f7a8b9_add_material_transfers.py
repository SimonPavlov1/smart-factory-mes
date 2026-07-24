"""add explicit material transfers

Revision ID: c4d5e6f7a8b9
Revises: 8f1d2c3b4a5e
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "8f1d2c3b4a5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "material_transfers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("source_task_id", sa.Integer(), nullable=True),
        sa.Column("issue_task_id", sa.Integer(), nullable=False),
        sa.Column("receive_task_id", sa.Integer(), nullable=True),
        sa.Column("source_task_type", sa.String(), nullable=True),
        sa.Column("recipient_role", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="reserved"),
        sa.Column("issued_by_user_id", sa.Integer(), nullable=True),
        sa.Column("accepted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["accepted_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["issue_task_id"], ["workflow_tasks.id"]),
        sa.ForeignKeyConstraint(["issued_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["receive_task_id"], ["workflow_tasks.id"]),
        sa.ForeignKeyConstraint(["source_task_id"], ["workflow_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issue_task_id"),
        sa.UniqueConstraint("receive_task_id"),
    )
    for column in ("id", "order_id", "source_task_id", "issue_task_id", "receive_task_id", "source_task_type", "recipient_role", "status"):
        op.create_index(op.f(f"ix_material_transfers_{column}"), "material_transfers", [column], unique=False)

    op.create_table(
        "material_transfer_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transfer_id", sa.Integer(), nullable=False),
        sa.Column("component_id", sa.Integer(), nullable=False),
        sa.Column("line_uid", sa.String(), nullable=True),
        sa.Column("requested_qty", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reserved_qty", sa.Float(), nullable=False, server_default="0"),
        sa.Column("issued_qty", sa.Float(), nullable=False, server_default="0"),
        sa.Column("accepted_qty", sa.Float(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["component_id"], ["components.id"]),
        sa.ForeignKeyConstraint(["transfer_id"], ["material_transfers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("id", "transfer_id", "component_id", "line_uid"):
        op.create_index(op.f(f"ix_material_transfer_lines_{column}"), "material_transfer_lines", [column], unique=False)


def downgrade() -> None:
    op.drop_table("material_transfer_lines")
    op.drop_table("material_transfers")
