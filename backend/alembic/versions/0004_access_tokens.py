"""add cluster access-token tracking and app_settings

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-24 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clusters", sa.Column("token_id", sa.String(64), nullable=True))
    op.add_column("clusters", sa.Column("token_expires_at", sa.DateTime(), nullable=True))
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("access_token_lifetime_days", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_column("clusters", "token_expires_at")
    op.drop_column("clusters", "token_id")
