"""add order quantity adjustment audit and surplus marker

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
"""

from alembic import op
import sqlalchemy as sa


revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    order_columns = {column["name"] for column in inspector.get_columns("orders")}
    item_columns = {column["name"] for column in inspector.get_columns("items")}
    if "adjustment_history" not in order_columns:
        op.add_column("orders", sa.Column("adjustment_history", sa.JSON(), nullable=True))
    if "is_order_surplus" not in item_columns:
        op.add_column(
            "items",
            sa.Column("is_order_surplus", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    item_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("items")}
    if "ix_items_is_order_surplus" not in item_indexes:
        op.create_index("ix_items_is_order_surplus", "items", ["is_order_surplus"])


def downgrade():
    op.drop_index("ix_items_is_order_surplus", table_name="items")
    op.drop_column("items", "is_order_surplus")
    op.drop_column("orders", "adjustment_history")
