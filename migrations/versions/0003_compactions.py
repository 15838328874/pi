"""add compactions (episodic memory summaries)

Revision ID: 0003_compactions
Revises: 0002_user_active
Create Date: 2026-09-03

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_compactions"
down_revision = "0002_user_active"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "compactions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(16),
            sa.ForeignKey("sessions.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("covered_upto_idx", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("compactions")
