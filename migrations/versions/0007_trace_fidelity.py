"""execution traces: full-fidelity run link

Revision ID: 0007_trace_fidelity
Revises: 0006_agent_runs
Create Date: 2026-09-05

Makes the trace answer "what exactly happened" on its own: the run row gains
the prompt verbatim, the request_id (X-Request-Id correlation with the access
log), the model-native capability flags, and the message-index range it wrote;
the step rows gain full tool arguments and full results (TEXT, was a 400-char
preview). A wrong-flag reproduction no longer needs a second table.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007_trace_fidelity"
down_revision = "0006_agent_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MySQL forbids defaults on TEXT columns; the ORM always supplies a value,
    # and ALTER fills existing rows with the implicit '' default.
    op.add_column("agent_runs", sa.Column("prompt", sa.Text(), nullable=False))
    op.add_column("agent_runs", sa.Column("request_id", sa.String(16), nullable=False, server_default=""))
    op.add_column(
        "agent_runs", sa.Column("enable_search", sa.Boolean(), nullable=False, server_default="0")
    )
    op.add_column(
        "agent_runs", sa.Column("builtin_tools", sa.String(64), nullable=False, server_default="")
    )
    op.add_column("agent_runs", sa.Column("first_idx", sa.Integer(), nullable=True))
    op.add_column("agent_runs", sa.Column("last_idx", sa.Integer(), nullable=True))
    op.add_column("agent_steps", sa.Column("args", sa.Text(), nullable=False))
    op.alter_column(
        "agent_steps",
        "detail",
        existing_type=sa.String(400),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "agent_steps",
        "detail",
        existing_type=sa.Text(),
        type_=sa.String(400),
        existing_nullable=False,
    )
    op.drop_column("agent_steps", "args")
    op.drop_column("agent_runs", "last_idx")
    op.drop_column("agent_runs", "first_idx")
    op.drop_column("agent_runs", "builtin_tools")
    op.drop_column("agent_runs", "enable_search")
    op.drop_column("agent_runs", "request_id")
    op.drop_column("agent_runs", "prompt")
