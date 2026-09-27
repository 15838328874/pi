"""add runs (structured trajectory index, jsonl remains the fallback copy)

Revision ID: 0005_runs
Revises: 0004_memories
Create Date: 2026-09-28

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_runs"
down_revision = "0004_memories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(12), primary_key=True),
        sa.Column("session_id", sa.String(16), nullable=False, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("trajectory", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("runs")
