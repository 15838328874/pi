"""add sessions.plan

Revision ID: 0003_session_plan
Revises: 0002_user_active
Create Date: 2026-09-04

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_session_plan"
down_revision = "0002_user_active"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no server_default, which departs from every other column in
    # this schema. It is the only portable shape for a TEXT column: MySQL rejects
    # a literal DEFAULT on TEXT/BLOB/JSON outright (error 1101, expression
    # defaults only since 8.0.13), while PostgreSQL rejects ADD COLUMN ... TEXT
    # NOT NULL on a table that already has rows unless a default is supplied.
    # NULL also carries the right meaning - "no plan yet".
    op.add_column("sessions", sa.Column("plan", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "plan")
