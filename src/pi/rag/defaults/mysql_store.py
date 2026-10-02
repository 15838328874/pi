"""MySQL ChunkStore - the production SQL source of truth.

Same rag_docs / rag_chunks shape as SqliteChunkStore (and the alembic migration
0008_rag), so rebuild-index and the ingest pipeline behave identically across
backends. This is kernel code (pi.rag.*): it must NOT import pi.server.db - it
takes a SQLAlchemy AsyncEngine (or a URL) directly and speaks raw SQL. The pi
integration wires the shared engine in via adapters.py (M5).

House rules honored:
- ACL: every read/write is WHERE user_id = :uid. user_id is untrusted caller
  input; it is bound as a parameter, never string-interpolated.
- The vector index (Milvus) is a rebuildable projection of THIS store; the
  rag_chunks.id PK doubles as the vector PK so hits hydrate 1:1.
- Async throughout (aiomysql); no to_thread needed - SQLAlchemy async does the
  off-loop work. Blocking stays out of the event loop by construction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pi.rag.defaults._like import escape_like
from pi.rag.types import Chunk, DocMeta, RetrievedChunk

log = logging.getLogger("pi.rag.mysql_store")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _engine_kwargs(url: str) -> dict[str, Any]:
    """utf8mb4 for full CJK/emoji; recycle below server wait_timeout so pooled
    connections never go stale (mirrors pi.server.db.engine_kwargs)."""
    if url.startswith("mysql"):
        return {"connect_args": {"charset": "utf8mb4"}, "pool_recycle": 280}
    return {}


# Idempotent DDL so the store is usable standalone (tests, single-node deploys)
# without running alembic. The migration 0008_rag is the canonical schema for
# the pi server; CREATE TABLE IF NOT EXISTS keeps both paths consistent.
# Indexes are declared INLINE (MySQL 8 rejects standalone CREATE INDEX IF NOT
# EXISTS), so the whole schema stays idempotent under one IF NOT EXISTS guard.
_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS rag_docs (
        id          BIGINT       NOT NULL AUTO_INCREMENT,
        user_id     BIGINT       NOT NULL,
        doc_key     VARCHAR(512) NOT NULL,
        title       VARCHAR(512) NOT NULL DEFAULT '',
        source_path VARCHAR(1024) NOT NULL DEFAULT '',
        visibility  VARCHAR(32)  NOT NULL DEFAULT 'private',
        status      VARCHAR(32)  NOT NULL DEFAULT 'pending',
        error       TEXT         NOT NULL,
        created_at  VARCHAR(32)  NOT NULL DEFAULT '',
        PRIMARY KEY (id),
        UNIQUE KEY uq_rag_docs_user_key (user_id, doc_key),
        KEY idx_rag_docs_user (user_id)
    ) DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS rag_chunks (
        id         BIGINT       NOT NULL AUTO_INCREMENT,
        doc_key    VARCHAR(512) NOT NULL,
        user_id    BIGINT       NOT NULL,
        seq        INT          NOT NULL,
        text       MEDIUMTEXT   NOT NULL,
        embed_text MEDIUMTEXT   NOT NULL,
        title_path VARCHAR(1024) NOT NULL DEFAULT '',
        page       INT          NULL,
        created_at VARCHAR(32)  NOT NULL DEFAULT '',
        PRIMARY KEY (id),
        KEY idx_rag_chunks_user (user_id),
        KEY idx_rag_chunks_doc (doc_key)
    ) DEFAULT CHARSET=utf8mb4
    """,
]


class MysqlChunkStore:
    """ChunkStore backed by MySQL via SQLAlchemy async (aiomysql).

    Pass either an existing AsyncEngine (pi integration shares one) or a URL
    (standalone). ``create_schema=True`` runs the idempotent DDL on first use.
    """

    def __init__(
        self,
        url_or_engine: str | AsyncEngine,
        *,
        create_schema: bool = True,
    ) -> None:
        if isinstance(url_or_engine, AsyncEngine):
            self.engine = url_or_engine
            self._owns_engine = False
        else:
            self.engine = create_async_engine(
                url_or_engine, pool_pre_ping=True, **_engine_kwargs(url_or_engine)
            )
            self._owns_engine = True
        self._create_schema = create_schema
        self._schema_ready = False

    async def _ensure_schema(self) -> None:
        if self._schema_ready or not self._create_schema:
            self._schema_ready = True
            return
        async with self.engine.begin() as conn:
            for ddl in _SCHEMA:
                await conn.execute(text(ddl))
        self._schema_ready = True

    async def dispose(self) -> None:
        if self._owns_engine:
            await self.engine.dispose()

    # -- docs ---------------------------------------------------------------

    async def upsert_doc(self, doc: DocMeta, status: str, error: str = "") -> int:
        await self._ensure_schema()
        # MySQL has no ON CONFLICT; use INSERT ... ON DUPLICATE KEY UPDATE keyed
        # on the UNIQUE(user_id, doc_key). LAST_INSERT_ID(id) makes the id
        # available on both the insert and the update path.
        sql = text(
            """
            INSERT INTO rag_docs
              (user_id, doc_key, title, source_path, visibility, status, error, created_at)
            VALUES
              (:uid, :key, :title, :path, :vis, :status, :error, :created)
            ON DUPLICATE KEY UPDATE
              title=VALUES(title), source_path=VALUES(source_path),
              visibility=VALUES(visibility), status=VALUES(status),
              error=VALUES(error), id=LAST_INSERT_ID(id)
            """
        )
        async with self.engine.begin() as conn:
            await conn.execute(
                sql,
                {
                    "uid": int(doc.user_id),
                    "key": doc.doc_key,
                    "title": doc.title,
                    "path": doc.source_path,
                    "vis": doc.visibility,
                    "status": status,
                    "error": error or "",
                    "created": _now(),
                },
            )
            row = (await conn.execute(text("SELECT LAST_INSERT_ID()"))).scalar_one()
            return int(row)

    async def get_doc(self, user_id: int, doc_key: str) -> dict | None:
        await self._ensure_schema()
        sql = text("SELECT * FROM rag_docs WHERE user_id=:uid AND doc_key=:key")
        async with self.engine.connect() as conn:
            res = await conn.execute(sql, {"uid": int(user_id), "key": doc_key})
            row = res.mappings().first()
            return dict(row) if row else None

    async def list_docs(self, user_id: int) -> list[dict]:
        await self._ensure_schema()
        sql = text("SELECT * FROM rag_docs WHERE user_id=:uid ORDER BY id")
        async with self.engine.connect() as conn:
            res = await conn.execute(sql, {"uid": int(user_id)})
            return [dict(r) for r in res.mappings().all()]

    async def delete_doc(self, user_id: int, doc_key: str) -> int:
        """Delete doc + its chunks (cascade). Returns chunks deleted."""
        await self._ensure_schema()
        async with self.engine.begin() as conn:
            n = (
                await conn.execute(
                    text("DELETE FROM rag_chunks WHERE user_id=:uid AND doc_key=:key"),
                    {"uid": int(user_id), "key": doc_key},
                )
            ).rowcount
            await conn.execute(
                text("DELETE FROM rag_docs WHERE user_id=:uid AND doc_key=:key"),
                {"uid": int(user_id), "key": doc_key},
            )
            return int(n or 0)

    async def mark_stale_pending(self, reason: str) -> int:
        """Startup sweep: pending rows have no live job after a restart."""
        await self._ensure_schema()
        async with self.engine.begin() as conn:
            res = await conn.execute(
                text(
                    "UPDATE rag_docs SET status='failed', error=:reason "
                    "WHERE status='pending'"
                ),
                {"reason": reason},
            )
            return int(res.rowcount or 0)

    # -- chunks -------------------------------------------------------------

    async def delete_chunks(self, user_id: int, doc_key: str) -> int:
        await self._ensure_schema()
        async with self.engine.begin() as conn:
            n = (
                await conn.execute(
                    text("DELETE FROM rag_chunks WHERE user_id=:uid AND doc_key=:key"),
                    {"uid": int(user_id), "key": doc_key},
                )
            ).rowcount
            return int(n or 0)

    async def replace_chunks(
        self, user_id: int, doc_key: str, chunks: list[Chunk]
    ) -> list[int]:
        """Atomically replace one doc's chunks (DELETE + INSERT, one txn).

        Re-ingest = delete-then-insert must be a SINGLE transaction or a
        concurrent reader can observe the doc with zero chunks mid-window and
        silently miss it. Reuses the same id-read-back mapping as add_chunks:
        after the insert we SELECT real ids by (user_id, doc_key, seq) rather
        than trusting LAST_INSERT_ID()+i (innodb_autoinc_lock_mode=2 interleaves
        concurrent bulk inserts).
        """
        await self._ensure_schema()
        if not chunks:
            # No chunks to insert: still delete the old set atomically.
            async with self.engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM rag_chunks WHERE user_id=:uid AND doc_key=:key"),
                    {"uid": int(user_id), "key": doc_key},
                )
            return []
        cols = ("doc_key", "user_id", "seq", "text", "embed_text", "title_path", "page", "created_at")
        values_clauses = []
        params: dict[str, Any] = {"uid": int(user_id), "key": doc_key}
        now = _now()
        for i, ch in enumerate(chunks):
            values_clauses.append(
                f"(:key{i}, :uid{i}, :seq{i}, :text{i}, :emb{i}, :path{i}, :page{i}, :created{i})"
            )
            params.update(
                {
                    f"key{i}": ch.doc_key,
                    f"uid{i}": int(ch.user_id),
                    f"seq{i}": int(ch.seq),
                    f"text{i}": ch.text,
                    f"emb{i}": ch.embed_text or ch.text,
                    f"path{i}": ch.title_path,
                    f"page{i}": ch.page,
                    f"created{i}": now,
                }
            )
        insert_sql = text(
            f"INSERT INTO rag_chunks ({','.join(cols)}) VALUES {','.join(values_clauses)}"
        )
        async with self.engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM rag_chunks WHERE user_id=:uid AND doc_key=:key"),
                {"uid": int(user_id), "key": doc_key},
            )
            await conn.execute(insert_sql, params)

        # Read the real ids back, keyed by (user_id, doc_key, seq).
        id_by_pos: dict[tuple[int, str, int], int] = {}
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT id, seq FROM rag_chunks "
                    "WHERE user_id=:uid AND doc_key=:key ORDER BY seq"
                ),
                {"uid": int(user_id), "key": doc_key},
            )
            for row in res.mappings().all():
                id_by_pos[(int(user_id), doc_key, int(row["seq"]))] = int(row["id"])
        ids = [id_by_pos[(int(c.user_id), c.doc_key, int(c.seq))] for c in chunks]
        if len(set(ids)) != len(ids):
            raise RuntimeError(
                f"replace_chunks id mapping collision: {len(ids)} chunks -> {len(set(ids))} ids"
            )
        return ids

    async def add_chunks(self, chunks: list[Chunk]) -> list[int]:
        """Persist chunks (chunk_id unset); return assigned ids in input order.

        One multi-row INSERT, then SELECT the ids back keyed by (user_id,
        doc_key, seq). We deliberately do NOT assume auto-increment ids are
        contiguous: MySQL's default innodb_autoinc_lock_mode=2 (interleaved)
        can interleave ids across concurrent bulk inserts, so LAST_INSERT_ID()+i
        would silently mis-map chunk_id <-> vector. Reading the real ids back is
        robust under concurrency (seq is unique within a doc, and ingest owns
        the doc's chunk set via delete-then-insert).
        """
        await self._ensure_schema()
        if not chunks:
            return []
        cols = ("doc_key", "user_id", "seq", "text", "embed_text", "title_path", "page", "created_at")
        values_clauses = []
        params: dict[str, Any] = {}
        now = _now()
        for i, ch in enumerate(chunks):
            values_clauses.append(
                f"(:key{i}, :uid{i}, :seq{i}, :text{i}, :emb{i}, :path{i}, :page{i}, :created{i})"
            )
            params.update(
                {
                    f"key{i}": ch.doc_key,
                    f"uid{i}": int(ch.user_id),
                    f"seq{i}": int(ch.seq),
                    f"text{i}": ch.text,
                    f"emb{i}": ch.embed_text or ch.text,
                    f"path{i}": ch.title_path,
                    f"page{i}": ch.page,
                    f"created{i}": now,
                }
            )
        insert_sql = text(
            f"INSERT INTO rag_chunks ({','.join(cols)}) VALUES {','.join(values_clauses)}"
        )
        async with self.engine.begin() as conn:
            await conn.execute(insert_sql, params)

        # Read the real ids back. Group by (user_id, doc_key) so a multi-doc
        # batch still maps correctly; within a group, order by seq matches the
        # caller's insertion order (ingest emits seq 0..n-1 per doc).
        keys = sorted({(int(c.user_id), c.doc_key) for c in chunks})
        id_by_pos: dict[tuple[int, str, int], int] = {}
        async with self.engine.connect() as conn:
            for uid, dk in keys:
                res = await conn.execute(
                    text(
                        "SELECT id, seq FROM rag_chunks "
                        "WHERE user_id=:uid AND doc_key=:key ORDER BY seq"
                    ),
                    {"uid": uid, "key": dk},
                )
                for row in res.mappings().all():
                    id_by_pos[(uid, dk, int(row["seq"]))] = int(row["id"])
        ids = [id_by_pos[(int(c.user_id), c.doc_key, int(c.seq))] for c in chunks]
        if len(set(ids)) != len(ids):
            raise RuntimeError(
                f"add_chunks id mapping collision: {len(ids)} chunks -> {len(set(ids))} ids"
            )
        return ids

    async def list_chunks_for_user(self, user_id: int) -> list[Chunk]:
        await self._ensure_schema()
        sql = text(
            "SELECT * FROM rag_chunks WHERE user_id=:uid ORDER BY doc_key, seq"
        )
        async with self.engine.connect() as conn:
            res = await conn.execute(sql, {"uid": int(user_id)})
            return [self._row_to_chunk(r) for r in res.mappings().all()]

    async def get_chunks_by_ids(self, chunk_ids: Sequence[int]) -> list[Chunk]:
        await self._ensure_schema()
        ids = [int(x) for x in chunk_ids]
        if not ids:
            return []
        # bindparam(expanding=True) -> safe IN (...) without string building
        sql = text("SELECT * FROM rag_chunks WHERE id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        )
        async with self.engine.connect() as conn:
            res = await conn.execute(sql, {"ids": ids})
            by_id = {int(r["id"]): self._row_to_chunk(r) for r in res.mappings().all()}
            return [by_id[i] for i in ids if i in by_id]

    async def search_text(self, user_id: int, query: str, k: int) -> list[RetrievedChunk]:
        """Last-resort SQL LIKE fallback (vector AND lexical both down).

        Matches title_path as well as text: the lexical channel indexes the
        heading path (Chunk.text_to_index), so the last resort must not be
        STRICTER than the thing it is backing up - a query for a term that
        lives only in a heading would otherwise return zero rows exactly when
        every other channel is already down.
        """
        await self._ensure_schema()
        # Escape LIKE metacharacters so a literal % / _ / \ in the query is
        # matched as a substring, not a wildcard (P1). MySQL's default escape
        # char IS backslash (NO_BACKSLASH_ESCAPES off), so no ESCAPE clause is
        # needed here - the backslashes escape_like emitted are honored as-is.
        like = f"%{escape_like(query.strip()[:64])}%"
        sql = text(
            """
            SELECT ch.*, d.title AS doc_title, d.source_path AS doc_source
            FROM rag_chunks ch
            LEFT JOIN rag_docs d ON d.user_id=ch.user_id AND d.doc_key=ch.doc_key
            WHERE ch.user_id=:uid
              AND (ch.text LIKE :like OR ch.title_path LIKE :like OR ch.embed_text LIKE :like)
            LIMIT :lim
            """
        )
        async with self.engine.connect() as conn:
            res = await conn.execute(
                sql, {"uid": int(user_id), "like": like, "lim": max(1, int(k))}
            )
            rows = res.mappings().all()
            out: list[RetrievedChunk] = []
            for i, r in enumerate(rows):
                out.append(
                    RetrievedChunk(
                        chunk_id=int(r["id"]),
                        doc_key=r["doc_key"],
                        text=r["text"],
                        score=float(len(rows) - i),  # crude positional score
                        title=r["doc_title"] or "",
                        title_path=r["title_path"] or "",
                        source=r["doc_source"] or "",
                        page=r["page"],
                    )
                )
            return out

    @staticmethod
    def _row_to_chunk(r: Any) -> Chunk:
        return Chunk(
            chunk_id=int(r["id"]),
            doc_key=r["doc_key"],
            user_id=int(r["user_id"]),
            seq=int(r["seq"]),
            text=r["text"],
            embed_text=r["embed_text"] or "",
            title_path=r["title_path"] or "",
            page=r["page"],
        )
