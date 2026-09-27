"""initial schema: users, sessions, messages, usage_records

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-31

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("quota_tokens", sa.Integer(), nullable=False, server_default="1000000"),
        sa.Column("created_at", sa.String(32), nullable=False),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(16), sa.ForeignKey("sessions.id"), nullable=False, index=True),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("blocks", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
    )
    op.create_table(
        "usage_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("username", sa.String(64), nullable=False, index=True),
        sa.Column("session_id", sa.String(16), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("est_cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("turns", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.String(32), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("usage_records")
    op.drop_table("messages")
    op.drop_table("sessions")
    op.drop_table("users")
