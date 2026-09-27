"""add memories (semantic cross-session memory)

Revision ID: 0004_memories
Revises: 0003_compactions
Create Date: 2026-09-03

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_memories"
down_revision = "0003_compactions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("memories")
