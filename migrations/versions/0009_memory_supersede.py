"""add memories.superseded_by (fact succession / 版本化退役)

Revision ID: 0009_memory_supersede
Revises: 0008_rag
Create Date: 2026-10-04

A memory update no longer overwrites the old row; it writes a new row and marks
the old one superseded (superseded_by → new row id). Retrieval filters to
``superseded_by IS NULL`` (current truth) while history is retained, so the
system can answer "what was it before" and a wrong overwrite is reversible.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_memory_supersede"
down_revision = "0008_rag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column(
            "superseded_by",
            sa.Integer(),
            sa.ForeignKey("memories.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("memories", "superseded_by")
