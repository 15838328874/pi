"""add audit_events (MySQL source of truth for the audit log)

Revision ID: 0005_audit_events
Revises: 0004_user_memories
Create Date: 2026-09-05

The JSONL file becomes a mirror: /v1/admin/audit reads this table, so audit
history stops rotating away with the daily file. `actor` normalizes the
user/username field the records carry (it is what deregistration erasure and
the admin user filter key on); `payload` is the record verbatim, so new record
fields need no migration.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_audit_events"
down_revision = "0004_user_memories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.String(32), nullable=False, index=True),
        sa.Column("event", sa.String(16), nullable=False, index=True),
        sa.Column("actor", sa.String(64), nullable=False, server_default="", index=True),
        sa.Column("tool", sa.String(64), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_events")
