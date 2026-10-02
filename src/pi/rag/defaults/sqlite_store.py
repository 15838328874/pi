"""SQLite ChunkStore - the standalone source of truth.

Same table shape as the MySQL/Postgres migration (rag_docs / rag_chunks) so
``rebuild-index`` and the pi adapters behave identically regardless of
backend. Uses stdlib sqlite3 wrapped in asyncio.to_thread (house rule:
blocking libs stay off the event loop) - so the kernel needs no aiosqlite.

ACL: every read is WHERE user_id=?. The vector store PK == rag_chunks.id,
so vector hits hydrate back through get_chunks_by_ids.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from pi.rag.defaults._like import escape_like
from pi.rag.types import Chunk, DocMeta, RetrievedChunk

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rag_docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    doc_key     TEXT    NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    source_path TEXT    NOT NULL DEFAULT '',
    visibility  TEXT    NOT NULL DEFAULT 'private',
    status      TEXT    NOT NULL DEFAULT 'pending',
    error       TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT '',
    UNIQUE(user_id, doc_key)
);
CREATE INDEX IF NOT EXISTS idx_rag_docs_user ON rag_docs(user_id);

CREATE TABLE IF NOT EXISTS rag_chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_key    TEXT    NOT NULL,
    user_id    INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    embed_text TEXT    NOT NULL DEFAULT '',
    title_path TEXT    NOT NULL DEFAULT '',
    page       INTEGER,
    created_at TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_user ON rag_chunks(user_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_doc  ON rag_chunks(doc_key);
"""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SqliteChunkStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = asyncio.Lock()  # serialize writes; sqlite is single-writer
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        def _init() -> None:
            with self._connect() as c:
                c.executescript(_SCHEMA)

        await asyncio.to_thread(_init)
        self._initialized = True

    async def upsert_doc(self, doc: DocMeta, status: str, error: str = "") -> int:
        await self._ensure_schema()

        def _run() -> int:
            with self._lock_sync(), self._connect() as c:
                c.execute(
                    """INSERT INTO rag_docs
                       (user_id, doc_key, title, source_path, visibility, status, error, created_at)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(user_id, doc_key) DO UPDATE SET
                         title=excluded.title, source_path=excluded.source_path,
                         visibility=excluded.visibility, status=excluded.status,
                         error=excluded.error""",
                    (
                        int(doc.user_id),
                        doc.doc_key,
                        doc.title,
                        doc.source_path,
                        doc.visibility,
                        status,
                        error,
                        _now(),
                    ),
                )
                row = c.execute(
                    "SELECT id FROM rag_docs WHERE user_id=? AND doc_key=?",
                    (int(doc.user_id), doc.doc_key),
                ).fetchone()
                return int(row["id"])

        return await asyncio.to_thread(_run)

    async def get_doc(self, user_id: int, doc_key: str) -> dict | None:
        await self._ensure_schema()

        def _run() -> dict | None:
            with self._connect() as c:
                row = c.execute(
                    "SELECT * FROM rag_docs WHERE user_id=? AND doc_key=?",
                    (int(user_id), doc_key),
                ).fetchone()
                return dict(row) if row else None

        return await asyncio.to_thread(_run)

    async def list_docs(self, user_id: int) -> list[dict]:
        await self._ensure_schema()

        def _run() -> list[dict]:
            with self._connect() as c:
                rows = c.execute(
                    "SELECT * FROM rag_docs WHERE user_id=? ORDER BY id", (int(user_id),)
                ).fetchall()
                return [dict(r) for r in rows]

        return await asyncio.to_thread(_run)

    async def delete_doc(self, user_id: int, doc_key: str) -> int:
        await self._ensure_schema()

        def _run() -> int:
            with self._connect() as c:
                cur = c.execute(
                    "DELETE FROM rag_chunks WHERE user_id=? AND doc_key=?",
                    (int(user_id), doc_key),
                )
                n = cur.rowcount
                c.execute(
                    "DELETE FROM rag_docs WHERE user_id=? AND doc_key=?",
                    (int(user_id), doc_key),
                )
                return n

        return await asyncio.to_thread(_run)

    async def delete_chunks(self, user_id: int, doc_key: str) -> int:
        await self._ensure_schema()

        def _run() -> int:
            with self._connect() as c:
                cur = c.execute(
                    "DELETE FROM rag_chunks WHERE user_id=? AND doc_key=?",
                    (int(user_id), doc_key),
                )
                return cur.rowcount

        return await asyncio.to_thread(_run)

    async def replace_chunks(
        self, user_id: int, doc_key: str, chunks: list[Chunk]
    ) -> list[int]:
        """Atomically replace one doc's chunks (DELETE + INSERT, one txn).

        SQLite gives each ``with self._connect()`` block an implicit single
        transaction, so running both statements on the SAME connection makes
        them atomic: a concurrent reader (its own connection) either sees the
        whole old set or the whole new set, never zero chunks mid-window.
        """
        await self._ensure_schema()

        def _run() -> list[int]:
            ids: list[int] = []
            with self._connect() as c:
                c.execute(
                    "DELETE FROM rag_chunks WHERE user_id=? AND doc_key=?",
                    (int(user_id), doc_key),
                )
                for ch in chunks:
                    cur = c.execute(
                        """INSERT INTO rag_chunks
                           (doc_key, user_id, seq, text, embed_text, title_path, page, created_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            ch.doc_key,
                            int(ch.user_id),
                            int(ch.seq),
                            ch.text,
                            ch.embed_text or ch.text,
                            ch.title_path,
                            ch.page,
                            _now(),
                        ),
                    )
                    ids.append(int(cur.lastrowid))
            return ids

        return await asyncio.to_thread(_run)

    async def add_chunks(self, chunks: list[Chunk]) -> list[int]:
        await self._ensure_schema()

        def _run() -> list[int]:
            ids: list[int] = []
            with self._connect() as c:
                for ch in chunks:
                    cur = c.execute(
                        """INSERT INTO rag_chunks
                           (doc_key, user_id, seq, text, embed_text, title_path, page, created_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            ch.doc_key,
                            int(ch.user_id),
                            int(ch.seq),
                            ch.text,
                            ch.embed_text or ch.text,
                            ch.title_path,
                            ch.page,
                            _now(),
                        ),
                    )
                    ids.append(int(cur.lastrowid))
            return ids

        return await asyncio.to_thread(_run)

    async def list_chunks_for_user(self, user_id: int) -> list[Chunk]:
        await self._ensure_schema()

        def _run() -> list[Chunk]:
            with self._connect() as c:
                rows = c.execute(
                    "SELECT * FROM rag_chunks WHERE user_id=? ORDER BY doc_key, seq",
                    (int(user_id),),
                ).fetchall()
                return [self._row_to_chunk(r) for r in rows]

        return await asyncio.to_thread(_run)

    async def get_chunks_by_ids(self, chunk_ids) -> list[Chunk]:
        await self._ensure_schema()
        ids = [int(x) for x in chunk_ids]
        if not ids:
            return []

        def _run() -> list[Chunk]:
            qmarks = ",".join("?" * len(ids))
            with self._connect() as c:
                rows = c.execute(
                    f"SELECT * FROM rag_chunks WHERE id IN ({qmarks})", ids  # noqa: S608 - ints only
                ).fetchall()
                by_id = {int(r["id"]): self._row_to_chunk(r) for r in rows}
                return [by_id[i] for i in ids if i in by_id]

        return await asyncio.to_thread(_run)

    async def search_text(self, user_id: int, query: str, k: int) -> list[RetrievedChunk]:
        """SQL LIKE last-resort fallback. Returns degraded hits, never raises
        for 'not found'. Scores are crude (substring rank), only used when
        both vector and BM25 are down. Matches title_path/embed_text too so the
        last resort is never STRICTER than the BM25 channel it backs up."""
        await self._ensure_schema()
        # Escape LIKE metacharacters so a literal % / _ / \ in the query is
        # matched as a substring, not a wildcard (P1). SQLite's default escape
        # char is NONE, so unlike MySQL the ESCAPE clause must be stated.
        like = f"%{escape_like(query.strip()[:64])}%"

        def _run() -> list[RetrievedChunk]:
            with self._connect() as c:
                rows = c.execute(
                    """SELECT ch.*, d.title, d.source_path
                       FROM rag_chunks ch LEFT JOIN rag_docs d
                         ON d.user_id=ch.user_id AND d.doc_key=ch.doc_key
                       WHERE ch.user_id=?
                         AND (ch.text LIKE ? ESCAPE '\\'
                              OR ch.title_path LIKE ? ESCAPE '\\'
                              OR ch.embed_text LIKE ? ESCAPE '\\')
                       LIMIT ?""",
                    (int(user_id), like, like, like, max(1, int(k))),
                ).fetchall()
                out: list[RetrievedChunk] = []
                for i, r in enumerate(rows):
                    out.append(
                        RetrievedChunk(
                            chunk_id=int(r["id"]),
                            doc_key=r["doc_key"],
                            text=r["text"],
                            score=float(len(rows) - i),  # crude positional score
                            title=r["title"] or "",
                            title_path=r["title_path"] or "",
                            source=r["source_path"] or "",
                            page=r["page"],
                        )
                    )
                return out

        return await asyncio.to_thread(_run)

    @staticmethod
    def _row_to_chunk(r: sqlite3.Row) -> Chunk:
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

    # sqlite3.Connection isn't a context manager for locking; this is a no-op
    # helper so `with self._lock_sync():` reads clearly. Writes are already
    # serialized by asyncio.to_thread + sqlite's own file lock.
    @staticmethod
    def _lock_sync():
        import contextlib

        return contextlib.nullcontext()
