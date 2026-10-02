"""add rag_docs + rag_chunks (RAG source of truth)

Revision ID: 0008_rag
Revises: 0007_files
Create Date: 2026-09-28

The SQL tables are the SOURCE OF TRUTH for the RAG kernel; the Milvus
collection pi_rag_chunks is a rebuildable projection keyed by rag_chunks.id
(the vector PK). Same shape as SqliteChunkStore/MysqlChunkStore so ingest and
rebuild-index behave identically across backends.

No FK to users.id: rag_docs.user_id is a plain int (ACL scope), kept decoupled
so the kernel can run standalone without the users table. Indexes on user_id
(every read is WHERE user_id=?) and doc_key (delete_by_doc / re-ingest purge).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0008_rag"
down_revision = "0007_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rag_docs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("doc_key", sa.String(512), nullable=False),
        sa.Column("title", sa.String(512), nullable=False, server_default=""),
        sa.Column("source_path", sa.String(1024), nullable=False, server_default=""),
        sa.Column("visibility", sa.String(32), nullable=False, server_default="private"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False, server_default=""),
        sa.UniqueConstraint("user_id", "doc_key", name="uq_rag_docs_user_key"),
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_rag_docs_user", "rag_docs", ["user_id"])

    op.create_table(
        "rag_chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("doc_key", sa.String(512), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql"), nullable=False),
        sa.Column("embed_text", sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql"), nullable=False),
        sa.Column("title_path", sa.String(1024), nullable=False, server_default=""),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False, server_default=""),
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_rag_chunks_user", "rag_chunks", ["user_id"])
    op.create_index("idx_rag_chunks_doc", "rag_chunks", ["doc_key"])


def downgrade() -> None:
    op.drop_index("idx_rag_chunks_doc", table_name="rag_chunks")
    op.drop_index("idx_rag_chunks_user", table_name="rag_chunks")
    op.drop_table("rag_chunks")
    op.drop_index("idx_rag_docs_user", table_name="rag_docs")
    op.drop_table("rag_docs")
