"""add user task routing settings

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
"""

from alembic import op
import sqlalchemy as sa


revision = "c1d2e3f4a5b6"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("users")}
    with op.batch_alter_table("users") as batch_op:
        if "task_roles" not in columns:
            batch_op.add_column(sa.Column("task_roles", sa.JSON(), nullable=True))
        if "auto_tasks_enabled" not in columns:
            batch_op.add_column(sa.Column(
                "auto_tasks_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
            ))
        if "manual_assignment_enabled" not in columns:
            batch_op.add_column(sa.Column(
                "manual_assignment_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
            ))


def downgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("manual_assignment_enabled")
        batch_op.drop_column("auto_tasks_enabled")
        batch_op.drop_column("task_roles")
