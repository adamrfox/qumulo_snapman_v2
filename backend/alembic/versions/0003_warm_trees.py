"""add warm_trees

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-22 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "warm_trees",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("source_file_id", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("cluster_id", "source_file_id", name="uq_warm_trees_cluster_tree"),
    )


def downgrade() -> None:
    op.drop_table("warm_trees")
