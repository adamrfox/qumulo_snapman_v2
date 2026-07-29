"""add warm_trees last_swept_at/last_error

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-29 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("warm_trees", sa.Column("last_swept_at", sa.DateTime(), nullable=True))
    op.add_column("warm_trees", sa.Column("last_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("warm_trees", "last_error")
    op.drop_column("warm_trees", "last_swept_at")
