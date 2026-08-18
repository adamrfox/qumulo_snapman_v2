"""add warm_sweep_interval_minutes to app_settings

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-18 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column(
            "warm_sweep_interval_minutes", sa.Integer(), nullable=False, server_default="15"
        ),
    )
    op.alter_column("app_settings", "warm_sweep_interval_minutes", server_default=None)


def downgrade() -> None:
    op.drop_column("app_settings", "warm_sweep_interval_minutes")
