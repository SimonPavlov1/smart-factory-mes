"""add product unit tracking

Revision ID: a9b0c1d2e3f4
Revises: f7a8b9c0d1e2
"""
from alembic import op
import sqlalchemy as sa

revision = "a9b0c1d2e3f4"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("items")}
    additions = {
        "order_item_id": sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("order_items.id")),
        "product_id": sa.Column("product_id", sa.Integer(), sa.ForeignKey("product_types.id")),
        "assembly_task_id": sa.Column("assembly_task_id", sa.Integer(), sa.ForeignKey("workflow_tasks.id")),
        "assigned_user_id": sa.Column("assigned_user_id", sa.Integer(), sa.ForeignKey("users.id")),
        "status": sa.Column("status", sa.String(), nullable=False, server_default="planned"),
        "defect_note": sa.Column("defect_note", sa.Text()),
        "created_at": sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        "assembly_started_at": sa.Column("assembly_started_at", sa.DateTime()),
        "assembled_at": sa.Column("assembled_at", sa.DateTime()),
        "tested_at": sa.Column("tested_at", sa.DateTime()),
        "packed_at": sa.Column("packed_at", sa.DateTime()),
        "stocked_at": sa.Column("stocked_at", sa.DateTime()),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("items", column)
    item_indexes = {index["name"] for index in inspector.get_indexes("items")}
    for name in ("order_item_id", "product_id", "assembly_task_id", "assigned_user_id", "status"):
        index_name = f"ix_items_{name}"
        if index_name not in item_indexes:
            op.create_index(index_name, "items", [name])

    if not inspector.has_table("factory_number_sequences"):
        op.create_table(
            "factory_number_sequences",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("prefix", sa.String(), nullable=False),
            sa.Column("year", sa.Integer(), nullable=False),
            sa.Column("last_value", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint("prefix", "year", name="uq_factory_number_prefix_year"),
        )
    sequence_indexes = {
        index["name"]
        for index in sa.inspect(bind).get_indexes("factory_number_sequences")
    }
    if "ix_factory_number_sequences_prefix" not in sequence_indexes:
        op.create_index("ix_factory_number_sequences_prefix", "factory_number_sequences", ["prefix"])
    if "ix_factory_number_sequences_year" not in sequence_indexes:
        op.create_index("ix_factory_number_sequences_year", "factory_number_sequences", ["year"])


def downgrade():
    op.drop_table("factory_number_sequences")
    for name in ("stocked_at", "packed_at", "tested_at", "assembled_at", "assembly_started_at", "created_at",
                 "defect_note", "status", "assigned_user_id", "assembly_task_id", "product_id", "order_item_id"):
        op.drop_column("items", name)
