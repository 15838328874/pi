"""add agent_runs + agent_steps (execution traces in MySQL)

Revision ID: 0006_agent_runs
Revises: 0005_audit_events
Create Date: 2026-09-05

One row per POST /runs plus one row per recorded step (tool calls, plans,
compactions, errors - never text deltas). `flags` is the finalized anomaly
verdict, computed at write time so the admin anomaly filter is a predicate.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_agent_runs"
down_revision = "0005_audit_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(16), nullable=False, unique=True, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("username", sa.String(64), nullable=False, index=True),
        sa.Column("session_id", sa.String(16), nullable=False, index=True),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok", index=True),
        sa.Column("error", sa.String(256), nullable=False, server_default=""),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("turns", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_tools", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("flags", sa.String(64), nullable=False, server_default=""),
        sa.Column("started_at", sa.String(32), nullable=False, index=True),
        sa.Column("ended_at", sa.String(32), nullable=False, server_default=""),
    )
    op.create_table(
        "agent_steps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("agent_runs.id"), nullable=False, index=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(64), nullable=False, server_default=""),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("detail", sa.String(400), nullable=False, server_default=""),
        sa.Column("duration_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("ts", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("agent_steps")
    op.drop_table("agent_runs")
