"""add warm_trees held_reason

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-29 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("warm_trees", sa.Column("held_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("warm_trees", "held_reason")
