"""add user_memories (MySQL source of truth for long-term memory)

Revision ID: 0004_user_memories
Revises: 0003_session_plan
Create Date: 2026-09-05

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_user_memories"
down_revision = "0003_session_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New table, so the BLOB column can be NOT NULL with no default: unlike
    # sessions.plan there are no existing rows to backfill. The embedding blob
    # (packed float32, 4KB at 1024 dims) is what lets Milvus be dropped and
    # rebuilt from MySQL alone; the integer id doubles as the vector-store
    # primary key so a row and its index mirror can never duplicate.
    op.create_table(
        "user_memories",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("content", sa.String(1024), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("source_session", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("last_seen_at", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("milvus_synced", sa.Boolean(), nullable=False, server_default="0", index=True),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_memories")
