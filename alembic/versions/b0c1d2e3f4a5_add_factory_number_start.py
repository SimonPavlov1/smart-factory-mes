"""add configurable factory number start

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
"""
from alembic import op
import sqlalchemy as sa


revision = "b0c1d2e3f4a5"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("product_types")}
    if "factory_number_start" not in columns:
        op.add_column(
            "product_types",
            sa.Column(
                "factory_number_start",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
        )


def downgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("product_types")}
    if "factory_number_start" in columns:
        op.drop_column("product_types", "factory_number_start")
