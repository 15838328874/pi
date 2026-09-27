"""add audit_events (structured query mirror of the audit jsonl)

Revision ID: 0006_audit_events
Revises: 0005_runs
Create Date: 2026-09-28

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_audit_events"
down_revision = "0005_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.String(32), nullable=False, index=True),
        sa.Column("event", sa.String(16), nullable=False),
        sa.Column("username", sa.String(64), nullable=False, index=True),
        sa.Column("tool", sa.String(32), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_events")
