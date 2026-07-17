"""add job_type to inspect_jobs

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-17 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inspect_jobs",
        sa.Column("job_type", sa.String(32), nullable=False, server_default="inspect"),
    )


def downgrade() -> None:
    op.drop_column("inspect_jobs", "job_type")
