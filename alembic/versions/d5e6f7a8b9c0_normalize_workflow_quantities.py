"""normalize workflow quantities and command idempotency

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
"""
from alembic import op
import sqlalchemy as sa

revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "task_quantities" not in existing:
        op.create_table("task_quantities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("workflow_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE")),
        sa.Column("entity_type", sa.String(), nullable=False), sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("line_uid", sa.String(), nullable=False, server_default=""),
        sa.Column("requested_qty", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reserved_qty", sa.Float(), nullable=False, server_default="0"),
        sa.Column("purchased_qty", sa.Float(), nullable=False, server_default="0"),
        sa.Column("received_qty", sa.Float(), nullable=False, server_default="0"),
        sa.Column("issued_qty", sa.Float(), nullable=False, server_default="0"),
        sa.Column("accepted_qty", sa.Float(), nullable=False, server_default="0"),
        sa.Column("processed_qty", sa.Float(), nullable=False, server_default="0"),
        sa.Column("rejected_qty", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("task_id", "entity_type", "entity_id", "line_uid", name="uq_task_quantity_line"))
        op.create_index("ix_task_quantities_task_id", "task_quantities", ["task_id"])
        op.create_index("ix_task_quantities_order_id", "task_quantities", ["order_id"])
    if "material_batches" not in existing:
        op.create_table("material_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_task_id", sa.Integer(), sa.ForeignKey("workflow_tasks.id"), nullable=False),
        sa.Column("batch_type", sa.String(), nullable=False), sa.Column("external_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="recorded"),
        sa.Column("document_ref", sa.String()), sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("source_task_id", "external_key", name="uq_material_batch_source_key"))
        op.create_index("ix_material_batches_order_id", "material_batches", ["order_id"])
        op.create_index("ix_material_batches_source_task_id", "material_batches", ["source_task_id"])
    if "material_batch_lines" not in existing:
        op.create_table("material_batch_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("material_batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False), sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("line_uid", sa.String(), nullable=False, server_default=""),
        sa.Column("quantity", sa.Float(), nullable=False), sa.Column("rejected_qty", sa.Float(), nullable=False, server_default="0"),
            sa.UniqueConstraint("batch_id", "entity_type", "entity_id", "line_uid", name="uq_material_batch_line"))
        op.create_index("ix_material_batch_lines_batch_id", "material_batch_lines", ["batch_id"])
    if "workflow_commands" not in existing:
        op.create_table("workflow_commands",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("workflow_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False), sa.Column("command_type", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False), sa.Column("response_payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("task_id", "idempotency_key", name="uq_workflow_command_task_key"))
        op.create_index("ix_workflow_commands_task_id", "workflow_commands", ["task_id"])


def downgrade():
    op.drop_table("workflow_commands")
    op.drop_table("material_batch_lines")
    op.drop_table("material_batches")
    op.drop_table("task_quantities")
