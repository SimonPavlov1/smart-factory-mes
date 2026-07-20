"""add inventory movement audit log

Revision ID: 8f1d2c3b4a5e
Revises: 36731b32c090
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8f1d2c3b4a5e"
down_revision: Union[str, Sequence[str], None] = "36731b32c090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inventory_movements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("component_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("balance_after", sa.Float(), nullable=False),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("counterparty_user_id", sa.Integer(), nullable=True),
        sa.Column("counterparty_role", sa.String(), nullable=True),
        sa.Column("recipient", sa.String(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["component_id"], ["components.id"]),
        sa.ForeignKeyConstraint(["counterparty_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["product_types.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["workflow_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("id", "component_id", "product_id", "direction", "task_id", "order_id", "actor_user_id", "counterparty_user_id", "created_at"):
        op.create_index(op.f(f"ix_inventory_movements_{column}"), "inventory_movements", [column], unique=False)


def downgrade() -> None:
    op.drop_table("inventory_movements")
