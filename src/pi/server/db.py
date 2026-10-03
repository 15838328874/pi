"""Async persistence layer (SQLAlchemy 2.0): users, sessions, messages.

PI_DATABASE_URL selects the backend: mysql+aiomysql://... or
postgresql+asyncpg://... - the schema and queries are dialect-neutral.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, delete as sa_delete, func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pi.llm.embedding import EmbeddingClient
from pi.server.vectorstore import VectorStore

log = logging.getLogger("pi.server.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    quota_tokens: Mapped[int] = mapped_column(Integer, default=1_000_000)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(128), default="session")
    model: Mapped[str] = mapped_column(String(128), default="")
    cwd: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    blocks: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(128))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    est_cost_usd: Mapped[float] = mapped_column(default=0.0)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(32), default=_now, index=True)


class MemoryRow(Base):
    """Semantic memory: cross-session, per-user long-term notes.

    Unlike messages (session-scoped, chronological) this is a flat pool of facts
    the agent (or user) explicitly wants to keep across sessions. Retrieval is
    lexical today; a vector/embedding backend can slot in behind MemoryRepo later
    without changing the tool or the runner.
    """

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class CompactionRow(Base):
    """An episodic-memory summary of a session's older messages.

    Append-only and non-destructive: `messages` keeps the full raw history, while
    this row records "messages up to idx covered_upto_idx were summarized into
    summary". The next turn loads [summary] + [messages with idx > covered_upto_idx]
    instead of re-summarizing (and re-paying the LLM call) every turn.
    """

    __tablename__ = "compactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    covered_upto_idx: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


def engine_kwargs(url: str) -> dict[str, Any]:
    """Dialect-specific async-engine kwargs, shared by app boot and alembic.

    MySQL: charset utf8mb4 for full Unicode (the server default may be latin1
    depending on instance config), and idle-connection recycling below the
    usual server-side wait_timeout so pooled connections never go stale.
    """
    if url.startswith("mysql"):
        return {"connect_args": {"charset": "utf8mb4"}, "pool_recycle": 280}
    return {}


class Database:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(
            url, pool_pre_ping=True, **engine_kwargs(url)
        )
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


class UserRepo:
    def __init__(self, db: Database):
        self.db = db

    async def count(self) -> int:
        async with AsyncSession(self.db.engine) as s:
            from sqlalchemy import func
            return (await s.execute(select(func.count(User.id)))).scalar_one()

    async def create(
        self, username: str, password_hash: str, is_admin: bool, quota_tokens: int = 1_000_000
    ) -> User:
        user = User(
            username=username,
            password_hash=password_hash,
            is_admin=is_admin,
            quota_tokens=quota_tokens,
        )
        async with AsyncSession(self.db.engine) as s:
            s.add(user)
            await s.commit()
            await s.refresh(user)
            return user

    async def set_active(self, user_id: int, active: bool) -> None:
        async with AsyncSession(self.db.engine) as s:
            await s.execute(
                sa_update(User).where(User.id == user_id).values(is_active=active)
            )
            await s.commit()

    async def set_quota(self, user_id: int, quota_tokens: int) -> None:
        async with AsyncSession(self.db.engine) as s:
            await s.execute(
                sa_update(User).where(User.id == user_id).values(quota_tokens=quota_tokens)
            )
            await s.commit()

    async def set_admin(self, user_id: int, admin: bool) -> None:
        """No HTTP route calls this: admin is granted by writing the table directly."""
        async with AsyncSession(self.db.engine) as s:
            await s.execute(
                sa_update(User).where(User.id == user_id).values(is_admin=admin)
            )
            await s.commit()

    async def list_all(self, limit: int = 200) -> Sequence[User]:
        async with AsyncSession(self.db.engine) as s:
            return (
                (
                    await s.execute(
                        select(User).order_by(User.id).limit(limit)
                    )
                )
                .scalars()
                .all()
            )

    async def by_username(self, username: str) -> User | None:
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(select(User).where(User.username == username))
            ).scalar_one_or_none()

    async def by_id(self, user_id: int) -> User | None:
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(select(User).where(User.id == user_id))
            ).scalar_one_or_none()


class SessionRepo:
    def __init__(self, db: Database):
        self.db = db

    async def create(self, user_id: int, title: str, model: str, cwd_root: Path) -> SessionRow:
        # 会话级隔离：cwd 由 session id 派生（workspace_root/{session_id}），
        # 同用户多会话互不串文件，与沙箱复用池/归档的 session 粒度对齐。
        sid = uuid.uuid4().hex[:12]
        row = SessionRow(
            id=sid,
            user_id=user_id,
            title=title[:128],
            model=model,
            cwd=str(cwd_root / sid),
        )
        async with AsyncSession(self.db.engine) as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row

    async def for_user(self, user_id: int, session_id: str) -> SessionRow | None:
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(
                    select(SessionRow).where(
                        SessionRow.id == session_id, SessionRow.user_id == user_id
                    )
                )
            ).scalar_one_or_none()

    async def list_for_user(self, user_id: int, limit: int = 50) -> Sequence[SessionRow]:
        async with AsyncSession(self.db.engine) as s:
            rows = (
                await s.execute(
                    select(SessionRow)
                    .where(SessionRow.user_id == user_id)
                    .order_by(SessionRow.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return rows

    async def delete_for_user(self, user_id: int, session_id: str) -> bool:
        """Delete a session with its messages and compaction summaries, in one
        transaction. Usage records and trajectory jsonl stay: billing and the
        raw run log are append-only history, not session state."""
        async with AsyncSession(self.db.engine) as s:
            row = (
                await s.execute(
                    select(SessionRow).where(
                        SessionRow.id == session_id, SessionRow.user_id == user_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            await s.execute(
                sa_delete(MessageRow).where(MessageRow.session_id == session_id)
            )
            await s.execute(
                sa_delete(CompactionRow).where(CompactionRow.session_id == session_id)
            )
            await s.execute(sa_delete(SessionRow).where(SessionRow.id == session_id))
            await s.commit()
            return True

    async def count(self) -> int:
        async with AsyncSession(self.db.engine) as s:
            return (await s.execute(select(func.count(SessionRow.id)))).scalar_one()


class AuditEventRow(Base):
    """Structured query mirror of the audit jsonl (compliance copy stays file-based).

    Columns cover the admin-console filters (day/user/tool/event); the full
    record lives in ``data``. Writes are best-effort from AuditLogger's async
    hook - a DB outage never loses the jsonl copy.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(32), index=True)  # ISO, starts with the day
    event: Mapped[str] = mapped_column(String(16))  # auth / tool_call / compaction
    username: Mapped[str] = mapped_column(String(64), index=True)  # user or username
    tool: Mapped[str] = mapped_column(String(32))  # tool name or auth action
    allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    data: Mapped[str] = mapped_column(Text)  # full record JSON
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class AuditRepo:
    def __init__(self, db: Database):
        self.db = db

    async def save(self, record: dict[str, Any]) -> None:
        row = AuditEventRow(
            ts=str(record.get("ts") or "")[:32],
            event=str(record.get("event") or "")[:16],
            username=str(record.get("user") or record.get("username") or "")[:64],
            tool=str(record.get("tool") or record.get("action") or "")[:32],
            allowed=record.get("allowed"),
            ok=record.get("ok"),
            data=json.dumps(record, ensure_ascii=False),
        )
        async with AsyncSession(self.db.engine) as s:
            s.add(row)
            await s.commit()

    async def query(
        self,
        *,
        day: str,
        user: str | None = None,
        tool: str | None = None,
        event: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        stmt = select(AuditEventRow).where(AuditEventRow.ts.like(day + "%"))
        if user:
            stmt = stmt.where(AuditEventRow.username == user)
        if tool:
            stmt = stmt.where(AuditEventRow.tool == tool)
        if event:
            stmt = stmt.where(AuditEventRow.event == event)
        stmt = stmt.order_by(AuditEventRow.id.desc()).limit(min(limit, 500))
        async with AsyncSession(self.db.engine) as s:
            rows = (await s.execute(stmt)).scalars().all()
        return [json.loads(r.data) for r in rows]


class RunRow(Base):
    """Structured query index for canonical run trajectories.

    The jsonl file stays the append-only audit-grade copy (fail-soft, daily
    rotation); this table is the queryable mirror (by run_id / session_id /
    user_id). Both are written per run; a DB write failure must not fail the
    run (the jsonl copy still exists).
    """

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(12), primary_key=True)
    # 无 FK：轨迹是追加式历史（同 usage_records），会话删除后 run 记录保留
    session_id: Mapped[str] = mapped_column(String(16), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    trajectory: Mapped[str] = mapped_column(Text)  # full to_dict() JSON
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class RunRepo:
    """Structured access to stored trajectories; jsonl remains the fallback."""

    def __init__(self, db: Database):
        self.db = db

    async def save(self, record: dict[str, Any]) -> None:
        row = RunRow(
            run_id=str(record.get("run_id") or uuid.uuid4().hex[:12]),
            session_id=str(record.get("session_id") or ""),
            user_id=int(record.get("user_id") or 0),
            trajectory=json.dumps(record, ensure_ascii=False),
        )
        async with AsyncSession(self.db.engine) as s:
            s.add(row)
            await s.commit()

    async def by_id(self, run_id: str) -> RunRow | None:
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(select(RunRow).where(RunRow.run_id == run_id))
            ).scalar_one_or_none()

    async def latest_for_session(self, session_id: str) -> RunRow | None:
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(
                    select(RunRow)
                    .where(RunRow.session_id == session_id)
                    .order_by(RunRow.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()


class MessageRepo:
    def __init__(self, db: Database):
        self.db = db

    async def append_many(self, session_id: str, entries: list[dict[str, Any]]) -> None:
        """entries: [{'idx': int, 'role': str, 'blocks': json-str}, ...]"""
        if not entries:
            return
        async with AsyncSession(self.db.engine) as s:
            for e in entries:
                s.add(
                    MessageRow(
                        session_id=session_id,
                        idx=e["idx"],
                        role=e["role"],
                        blocks=e["blocks"],
                    )
                )
            await s.commit()

    async def list_for_session(
        self, session_id: str, after_idx: int = -1
    ) -> Sequence[MessageRow]:
        """Messages in idx order; ``after_idx`` skips messages already summarized
        into an episodic-memory compaction record."""
        query = select(MessageRow).where(MessageRow.session_id == session_id)
        if after_idx >= 0:
            query = query.where(MessageRow.idx > after_idx)
        query = query.order_by(MessageRow.idx)
        async with AsyncSession(self.db.engine) as s:
            return (await s.execute(query)).scalars().all()

    async def count_for_session(self, session_id: str) -> int:
        async with AsyncSession(self.db.engine) as s:
            from sqlalchemy import func
            return (
                await s.execute(
                    select(func.count(MessageRow.id)).where(
                        MessageRow.session_id == session_id
                    )
                )
            ).scalar_one()

    async def save_compaction(
        self,
        session_id: str,
        covered_upto_idx: int,
        summary: str,
        model: str = "",
    ) -> None:
        async with AsyncSession(self.db.engine) as s:
            s.add(
                CompactionRow(
                    session_id=session_id,
                    covered_upto_idx=covered_upto_idx,
                    summary=summary,
                    model=model,
                )
            )
            await s.commit()

    async def latest_compaction(self, session_id: str) -> CompactionRow | None:
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(
                    select(CompactionRow)
                    .where(CompactionRow.session_id == session_id)
                    .order_by(CompactionRow.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()


#: Lexical Jaccard threshold above which a new memory is treated as a duplicate
#: of an existing one (short facts, so token overlap is reliable).
_DUP_JACCARD = 0.85
#: Cosine-similarity threshold for SEMANTIC dedup (the preferred path). Below
#: this two memories are treated as distinct even if they share words.
_DUP_SIMILARITY = 0.92
#: Per-user cap on memories. Keeps the pool bounded and top-k retrieval clean.
_MEMORY_LIMIT = 500


class MemoryRepo:
    """Semantic (cross-session) memory store.

    Retrieval is lexical by default (zero dependencies). When a vector store
    and an embedder are injected, ``search`` does vector retrieval first and
    falls back to lexical on any failure or empty result; ``add`` also upserts
    the vector. The ``memories`` table stays the source of truth either way.
    """

    def __init__(
        self,
        db: Database,
        vector_store: VectorStore | None = None,
        embedder: EmbeddingClient | None = None,
        on_embed_usage: "Callable[[int, int], Awaitable[None]] | None" = None,
        on_retrieval: "Callable[[str, float], Awaitable[None]] | None" = None,
        memory_limit: int = _MEMORY_LIMIT,
    ):
        self.db = db
        self.memory_limit = memory_limit
        self.vector_store = vector_store
        self.embedder = embedder
        # (user_id, tokens) after a successful embed call - the app wires this
        # to the usage tracker so embedding spend is metered like LLM tokens.
        self.on_embed_usage = on_embed_usage
        # (outcome, duration_s) per search - the app wires this to metrics so
        # a retrieval served by the lexical fallback (index down) makes noise.
        self.on_retrieval = on_retrieval

    async def add(self, user_id: int, text: str) -> bool:
        """Insert a memory. Returns True when inserted, False when it duplicated
        an existing one (nothing written).

        Two guards keep the pool clean as it grows (see docs/artifact-delivery
        and the memory-design notes):
          - dedup: a near-verbatim fact is not stored twice;
          - per-user cap: the oldest memories are evicted past ``_MEMORY_LIMIT``.

        Both are deliberate: ``add`` is called automatically (e.g. on context
        compaction), so without them the pool balloons into duplicates + stale
        facts and the top-k retrieval degrades into noise.
        """
        text = text.strip()
        if not text:
            return False
        # Embed ONCE and reuse the vector for both semantic dedup and the
        # Milvus upsert - never bill the same text twice. ``vec is None`` means
        # the vector path is off or the embed failed; lexical dedup still runs.
        vec: list[float] | None = None
        if self.embedder is not None and self.vector_store is not None:
            try:
                result = await self.embedder.embed([text])
                await self._meter_embed(user_id, result.usage_tokens)
                (vec,) = result.vectors
            except Exception:  # noqa: BLE001 - dedup must never fail a write
                log.exception("semantic dedup embed failed; falling back to lexical")
                vec = None
        if vec is not None:
            # Semantic dedup: nearest neighbour cosine ≥ threshold means the
            # meaning is already stored. A search failure here only SKIPS dedup
            # (lexical still runs) - keep ``vec`` so ``_vector_add`` does not
            # re-embed and double-bill a text that was already metered above.
            try:
                hits = await self.vector_store.search(user_id, vec, k=1)
                if hits and hits[0][1] >= _DUP_SIMILARITY:
                    return False  # semantic duplicate: same meaning, other wording
            except Exception:  # noqa: BLE001 - dedup must never fail a write
                log.exception("semantic dedup search failed; skipping dedup (lexical still runs)")
        # Lexical Jaccard runs AFTER semantic dedup for two reasons: it is the
        # only dedup when the vector path is off/failed, and it is a safety net
        # on top of a passing semantic check (catches near-verbatim repeats even
        # when embeddings are noisy). It cannot see semantic equivalence, which
        # is exactly why it must never be the PRIMARY check.
        if await self._is_duplicate_lexical(user_id, text):
            return False
        async with AsyncSession(self.db.engine) as s:
            row = MemoryRow(user_id=user_id, text=text)
            s.add(row)
            await s.flush()  # capture the autoincrement id for the vector key
            memory_id = row.id
            await s.commit()
        await self._vector_add(memory_id, user_id, text, vec)
        await self._enforce_limit(user_id, self.memory_limit)
        return True

    async def _is_duplicate_lexical(self, user_id: int, text: str) -> bool:
        """Lexical Jaccard fallback: token overlap against existing memories.

        Only catches near-verbatim repeats; cannot see semantic equivalence.
        """
        new_terms = _terms(text)
        if not new_terms:
            return False
        for r in await self.list_for_user(user_id):
            old_terms = _terms(r.text)
            if not old_terms:
                continue
            jaccard = len(new_terms & old_terms) / len(new_terms | old_terms)
            if jaccard >= _DUP_JACCARD:
                return True
        return False

    async def _enforce_limit(self, user_id: int, limit: int = _MEMORY_LIMIT) -> None:
        """Evict the OLDEST memories beyond the per-user cap.

        Vector side is left as-is: stale Milvus ids are already skipped by
        ``_rows_by_ids`` (it re-checks existence in MySQL), so orphan vectors
        are harmless - they only cost a tiny bit of index space and never
        surface as wrong answers.
        """
        async with AsyncSession(self.db.engine) as s:
            ids = (
                await s.execute(
                    select(MemoryRow.id)
                    .where(MemoryRow.user_id == user_id)
                    .order_by(MemoryRow.id.asc())  # oldest first
                )
            ).scalars().all()
            if len(ids) <= limit:
                return
            to_delete = ids[: len(ids) - limit]
            await s.execute(sa_delete(MemoryRow).where(MemoryRow.id.in_(to_delete)))
            await s.commit()

    async def list_for_user(self, user_id: int, limit: int = 500) -> Sequence[MemoryRow]:
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(
                    select(MemoryRow)
                    .where(MemoryRow.user_id == user_id)
                    .order_by(MemoryRow.id.desc())
                    .limit(limit)
                )
            ).scalars().all()

    async def search(self, user_id: int, query: str, k: int = 3) -> list[MemoryRow]:
        """Vector search when configured; lexical fallback otherwise and on any
        vector-path failure or empty result."""
        if self.embedder is None or self.vector_store is None:
            return await self._lexical_search(user_id, query, k)  # feature off
        t0 = time.perf_counter()
        hits, failure = await self._vector_search(user_id, query, k)
        ids = [mid for mid, _ in hits] if hits else []
        if ids:
            rows = await self._rows_by_ids(user_id, ids)
            if rows:
                await self._report_retrieval("vector_hit", time.perf_counter() - t0)
                return rows
        rows = await self._lexical_search(user_id, query, k)
        if failure == "embed_failed":
            outcome = "embed_failed"
        elif rows:
            outcome = "lexical_fallback"
        else:
            outcome = "no_hits"
        await self._report_retrieval(outcome, time.perf_counter() - t0)
        return rows

    async def _vector_add(
        self, memory_id: int, user_id: int, text: str, vec: list[float] | None = None
    ) -> None:
        """Best-effort vector upsert; failures are logged and swallowed so a
        memory write can never fail a run (the row is already committed).

        ``vec`` is the embedding already produced by ``add``; when None (vector
        path off, or the embed failed earlier) this falls back to embedding here
        once more so a transient embed outage still repopulates on a later write.
        """
        if self.embedder is None or self.vector_store is None:
            return
        try:
            if vec is None:
                result = await self.embedder.embed([text])
                await self._meter_embed(user_id, result.usage_tokens)
                (vec,) = result.vectors
            await self.vector_store.add(memory_id, user_id, text, vec)
        except Exception:  # noqa: BLE001 - memory must never fail a run
            log.exception(
                "vector memory add failed (memory_id=%s); lexical fallback only",
                memory_id,
            )

    async def _vector_search(
        self, user_id: int, query: str, k: int
    ) -> tuple[list[tuple[int, float]] | None, str | None]:
        """(hits, failure). hits are ``(memory_id, cosine)`` in descending
        similarity order; None on failure or empty result -> the caller falls
        back to lexical. failure distinguishes the two outage kinds:
        "embed_failed" (nothing was billed) vs "store_failed" (embed succeeded,
        the index is down - the more alarming one)."""
        if self.embedder is None or self.vector_store is None:
            return None, None
        try:
            result = await self.embedder.embed([query])
            # Metered immediately: an embed call that succeeded was billed even
            # if the index (and its fallback) dies right after.
            await self._meter_embed(user_id, result.usage_tokens)
            (vec,) = result.vectors
        except Exception:  # noqa: BLE001 - memory must never fail a run
            log.exception("vector memory search failed (embed); falling back to lexical")
            return None, "embed_failed"
        try:
            hits = await self.vector_store.search(user_id, vec, k)
        except Exception:  # noqa: BLE001 - memory must never fail a run
            log.exception("vector memory search failed (index); falling back to lexical")
            return None, "store_failed"
        return hits or None, None

    async def _report_retrieval(self, outcome: str, duration_s: float) -> None:
        if self.on_retrieval is None:
            return
        try:
            await self.on_retrieval(outcome, duration_s)
        except Exception:  # noqa: BLE001 - metrics must never fail memory
            log.exception("retrieval metrics reporting failed")

    async def _meter_embed(self, user_id: int, tokens: int) -> None:
        if self.on_embed_usage is None or not tokens:
            return
        try:
            await self.on_embed_usage(user_id, tokens)
        except Exception:  # noqa: BLE001 - accounting must never fail memory
            log.exception("embedding usage recording failed")

    async def _rows_by_ids(self, user_id: int, ids: list[int]) -> list[MemoryRow]:
        """Re-fetch rows in vector-hit order, skipping ids missing from the DB
        (stale Milvus entries) and ids not owned by this user (safety)."""
        async with AsyncSession(self.db.engine) as s:
            rows = (
                await s.execute(
                    select(MemoryRow).where(
                        MemoryRow.id.in_(ids), MemoryRow.user_id == user_id
                    )
                )
            ).scalars().all()
        by_id = {r.id: r for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    async def _lexical_search(self, user_id: int, query: str, k: int) -> list[MemoryRow]:
        """Top-k lexical retrieval (token overlap) - the original search body."""
        rows = list(await self.list_for_user(user_id))
        if not rows:
            return []
        qterms = _terms(query)
        if not qterms:
            return []
        scored: list[tuple[float, MemoryRow]] = []
        for r in rows:
            inter = qterms & _terms(r.text)
            if inter:
                scored.append((len(inter) / len(qterms), r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:k]]


_TERM_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")


def _terms(s: str) -> set[str]:
    return set(_TERM_RE.findall(s.lower()))


class FileRow(Base):
    """Object-storage file metadata: the source of truth lives in MinIO/S3,
    this table is the queryable index (who owns it, what it is, sha for dedup).

    sha256 dedup is user-scoped: same user uploading byte-identical content
    (incl. re-uploads) reuses the existing object+row instead of writing again.
    """

    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("user_id", "sha256", name="uk_user_sha"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    object_key: Mapped[str] = mapped_column(String(512), unique=True)
    bucket: Mapped[str] = mapped_column(String(64), default="pi-files")
    filename: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    content_type: Mapped[str] = mapped_column(String(128), default="")
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class FileRepo:
    def __init__(self, db: Database):
        self.db = db

    async def by_sha(self, user_id: int, sha256: str) -> FileRow | None:
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(
                    select(FileRow).where(FileRow.user_id == user_id, FileRow.sha256 == sha256)
                )
            ).scalar_one_or_none()

    async def by_id(self, file_id: int) -> FileRow | None:
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(select(FileRow).where(FileRow.id == file_id))
            ).scalar_one_or_none()

    async def by_key(self, object_key: str) -> FileRow | None:
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(select(FileRow).where(FileRow.object_key == object_key))
            ).scalar_one_or_none()

    async def create(
        self,
        *,
        user_id: int,
        object_key: str,
        bucket: str,
        filename: str,
        size: int,
        content_type: str,
        sha256: str,
    ) -> FileRow:
        row = FileRow(
            user_id=user_id,
            object_key=object_key,
            bucket=bucket,
            filename=filename,
            size=size,
            content_type=content_type,
            sha256=sha256,
        )
        async with AsyncSession(self.db.engine) as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row

    async def list_for_user(self, user_id: int) -> list[FileRow]:
        async with AsyncSession(self.db.engine) as s:
            rows = await s.execute(
                select(FileRow).where(FileRow.user_id == user_id).order_by(FileRow.created_at.desc())
            )
            return list(rows.scalars().all())

    async def remove(self, file_id: int) -> FileRow | None:
        """Delete the row and return it (so the caller can drop the object)."""
        async with AsyncSession(self.db.engine) as s:
            row = (
                await s.execute(select(FileRow).where(FileRow.id == file_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            await s.delete(row)
            await s.commit()
            return row
