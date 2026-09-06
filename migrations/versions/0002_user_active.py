"""add users.is_active

Revision ID: 0002_user_active
Revises: 0001_initial
Create Date: 2026-09-01

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_user_active"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("users", "is_active")
