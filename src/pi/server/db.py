"""Async persistence layer (SQLAlchemy 2.0): users, sessions, messages.

PI_DATABASE_URL selects the backend: mysql+aiomysql://... or
postgresql+asyncpg://... - the schema and queries are dialect-neutral.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, delete as sa_delete, func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pi.llm.embedding import EmbeddingClient
from pi.server.cache import CacheBackend
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

    ``superseded_by`` implements fact succession (Zep/Graphiti-style): a value
    change writes a NEW row and points the old row at it, instead of overwriting
    in place. Retrieval filters to ``superseded_by IS NULL`` (current truth);
    history stays in the table, so the past is answerable and a wrong overwrite
    is reversible.
    """

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    superseded_by: Mapped[int | None] = mapped_column(
        ForeignKey("memories.id", ondelete="SET NULL"), nullable=True
    )


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


#: Lexical Jaccard threshold for the NO-JUDGE fallback duplicate check (short
#: facts, so token overlap is reliable). It only runs when the judge is absent
#: or failed, and only catches near-verbatim repeats.
_DUP_JACCARD = 0.85
#: Per-user cap on memories. Keeps the pool bounded and top-k retrieval clean.
_MEMORY_LIMIT = 500
#: Redis lock TTL for the per-user ``add`` lock. Must outlive the whole add -
#: embed, Milvus search/upsert and the DB writes are each bounded by their own
#: timeout, so the lock is held for a bounded span. No renewal, matching the
#: session lock in runner.py: a crashed process just holds the key until TTL,
#: and memory writes are low-frequency so the wait is tolerable.
_MEM_LOCK_TTL = 60.0
#: How long to wait for a concurrent ``add`` on the same user before giving up
#: and writing anyway. Fail-open: an occasional duplicate is cheaper than a
#: silently lost memory (and the per-user cap still bounds the damage).
_MEM_LOCK_WAIT = 5.0
#: Retry interval between lock attempts.
_MEM_LOCK_RETRY = 0.05
#: Per-channel recall depth. Recall is the real bottleneck, not the judge:
#: embedding ranks by semantic/value words, so "用户0的主语言改成Go" pulls every
#: "用户X的主语言是Go" to the top and pushes the true target
#: ("用户0的主语言是Rust") to ~#5 — sometimes out of a top-5 entirely. Two
#: complementary channels each take this many:
#:   - vector  : semantic / value-word similarity
#:   - BM25/IDF: entity-word overlap (bigrams), which recovers the target the
#:               vector channel lost (measured: target lands in BM25 top-5).
_RECALL_K = 8
#: Hard cap on the merged (deduped) candidate list handed to the judge.
_RECALL_MAX = 12


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
        cache: CacheBackend | None = None,
        judge: "Callable[[int, str, list[str]], Awaitable[tuple[str, int | None]]] | None" = None,
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
        # Distributed per-user add lock (Redis-backed when configured); None
        # means the dedup check-then-write is NOT serialized across processes
        # (single-instance callers and unit tests pass nothing).
        self.cache = cache
        # Optional LLM judge: classifies (new_text, candidates) into
        # ("duplicate" | "conflict" | "new", target_index). It sees the WHOLE
        # top-k (not a reranker-picked top-1) and picks the target itself, so a
        # single wrong top-1 cannot mislead it. Absent → cosine + lexical only.
        self.judge = judge

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

        The dedup check-then-write is serialized per user via a distributed
        lock (Redis-backed when ``cache`` is injected) so two concurrent adds
        for the same user cannot both pass dedup. The lock is fail-open: if the
        backend is down or the wait times out, we write anyway - an occasional
        duplicate beats a silently lost memory.
        """
        text = text.strip()
        if not text:
            return False
        if not _terms(text):
            # Pure emoji / punctuation / symbols carry no retrievable tokens: a
            # memory nobody can ever search back is garbage. Reject like blank.
            return False
        acquired = await self._acquire_add_lock(user_id)
        lock_key, lock_token = acquired if acquired is not None else (None, None)
        try:
            return await self._add_locked(user_id, text)
        finally:
            if lock_key is not None:
                try:
                    await self.cache.release_lock_owned(lock_key, lock_token)
                except Exception:  # noqa: BLE001 - lock release must not hide the write
                    log.exception("memory add lock release failed (key=%s)", lock_key)

    async def _acquire_add_lock(self, user_id: int) -> tuple[str, str] | None:
        """Per-user add lock (distributed when Redis-backed). Returns (key, token)
        to release, or None when unlocked (no backend, or fail-open on error/timeout).

        The token makes release compare-and-delete: if our TTL expired and the
        key was re-acquired by another request, we cannot delete their lock.
        """
        if self.cache is None:
            return None
        key = f"mem:add:{user_id}"
        deadline = time.monotonic() + _MEM_LOCK_WAIT
        while True:
            try:
                token = await self.cache.acquire_lock_owned(key, _MEM_LOCK_TTL)
                if token is not None:
                    return key, token
            except Exception:  # noqa: BLE001 - Redis down must not block memory writes
                log.exception("memory add lock acquire failed; proceeding unlocked")
                return None
            if time.monotonic() >= deadline:
                log.warning(
                    "memory add lock wait exceeded %.1fs for user %s; proceeding unlocked",
                    _MEM_LOCK_WAIT,
                    user_id,
                )
                return None
            await asyncio.sleep(_MEM_LOCK_RETRY)

    async def _add_locked(self, user_id: int, text: str) -> bool:
        """Dedup (+conflict resolution) + insert + evict. Callers must hold the
        per-user add lock so the check-then-write is atomic.

        Pipeline: embed once → hybrid recall (vector ∪ BM25) → judge classifies
        duplicate/conflict/new against the whole candidate list →
          duplicate: no-op; conflict: supersede (new row + retire old); new: insert.
        No judge → lexical Jaccard fallback (near-verbatim repeats only).
        """
        vec = await self._embed_for_add(user_id, text)
        candidates = await self._recall_candidates(user_id, text, vec)
        verdict, conflict_mid = await self._resolve_verdict(user_id, text, candidates)
        if verdict == "duplicate":
            return False
        if verdict == "conflict" and conflict_mid is not None:
            await self._supersede(conflict_mid, user_id, text, vec)
            return True
        # "new": insert a fresh row.
        async with AsyncSession(self.db.engine) as s:
            row = MemoryRow(user_id=user_id, text=text)
            s.add(row)
            await s.flush()  # capture the autoincrement id for the vector key
            memory_id = row.id
            await s.commit()
        await self._vector_add(memory_id, user_id, text, vec)
        await self._enforce_limit(user_id, self.memory_limit)
        return True

    async def _embed_for_add(self, user_id: int, text: str) -> list[float] | None:
        """Embed ONCE and reuse the vector for recall, dedup and the Milvus
        upsert - never bill the same text twice. None when the vector path is off
        or the embed failed (lexical dedup still runs)."""
        if self.embedder is None or self.vector_store is None:
            return None
        try:
            result = await self.embedder.embed([text])
            await self._meter_embed(user_id, result.usage_tokens)
            (vec,) = result.vectors
            return vec
        except Exception:  # noqa: BLE001 - dedup must never fail a write
            log.exception("semantic dedup embed failed; falling back to lexical")
            return None

    async def _recall_candidates(
        self, user_id: int, text: str, vec: list[float] | None
    ) -> list[MemoryRow]:
        """Hybrid recall for the judge stage: vector (semantic) ∪ BM25 (entity).

        The two channels fail differently and that is the point. Vector recall
        groups by meaning, so a value word drags in every same-value neighbour
        and can push the true target out of the top-k; BM25 over character
        bigrams groups by ENTITY, so "用户0的主语言是Rust" survives even when the
        new text says "改成Go". Union + dedupe, capped at ``_RECALL_MAX``.
        """
        out: list[MemoryRow] = []
        seen: set[int] = set()

        # BM25 FIRST: it groups by entity, so the same-subject memory (the one
        # the judge must overwrite) leads the list. If vector recall led, a
        # value-word neighbour would sit at #0 and the judge could pick IT as the
        # conflict target — overwriting the wrong memory (measured: "用户0改成Go"
        # led with "用户1的主语言是Go" at #0 and the judge overwrote that).
        try:
            for r in await self._bm25_recall(user_id, text, _RECALL_K):
                if r.id not in seen:
                    seen.add(r.id)
                    out.append(r)
        except Exception:  # noqa: BLE001 - recall must never fail a write
            log.exception("bm25 recall failed; vector recall only")

        if vec is not None:
            try:
                hits = await self.vector_store.search(user_id, vec, _RECALL_K)
                if hits:
                    for r in await self._rows_by_ids(user_id, [mid for mid, _ in hits]):
                        if r.id not in seen:
                            seen.add(r.id)
                            out.append(r)
            except Exception:  # noqa: BLE001 - recall must never fail a write
                log.exception("vector recall failed; falling back to lexical")

        if out:
            return out[:_RECALL_MAX]
        # Last resort when the vector path is off/failed and BM25 found nothing.
        return await self._lexical_search(user_id, text, _RECALL_K)

    async def _bm25_recall(self, user_id: int, text: str, k: int) -> list[MemoryRow]:
        """IDF-weighted lexical recall over bigram+unigram terms (BM25-style).

        Purpose is COVERAGE, not scoring: an entity such as "用户0" appears as the
        bigram "户0" (rare → high IDF), so this channel surfaces the same-subject
        memory that semantic recall dropped. It deliberately does NOT decide
        duplicate/conflict — measured, both classes overlap on this score.
        """
        rows = list(await self.list_for_user(user_id))
        if not rows:
            return []
        qterms = _terms_bi(text)
        if not qterms:
            return []
        n = len(rows)
        doc_terms = [(r, _terms_bi(r.text)) for r in rows]
        df: Counter[str] = Counter()
        for _, ts in doc_terms:
            df.update(ts)
        scored: list[tuple[float, MemoryRow]] = []
        for r, ts in doc_terms:
            score = sum(math.log((n + 1) / (1 + df.get(t, 0))) for t in (qterms & ts))
            if score > 0:
                scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:k]]

    async def _resolve_verdict(
        self,
        user_id: int,
        text: str,
        candidates: list[MemoryRow],
    ) -> tuple[str, int | None]:
        """Decide duplicate / conflict / new for ``text`` against existing
        memories. Returns ``(verdict, conflict_memory_id)``.

        The judge sees the WHOLE top-k and decides all three classes — there is
        NO cheap score split. Measured on real data, "同义改写" (0.95+), "换值"
        (~0.88) and "同模板不同主体" (~0.89) OVERLAP in cosine, so any threshold
        either drops memories or leaks duplicates. Only the LLM can tell them
        apart (mem0-style: recall then hand everything to the model).
        """
        # 1. Judge stage: hand it the WHOLE top-k, not a single top-1. Embedding
        # recall can rank an interfering neighbour first ("改成Go" recalls
        # "用户1的主语言是Go" above "用户0的主语言是Rust"), so the judge must see
        # every candidate and pick the target itself.
        if self.judge is not None and candidates:
            try:
                verdict, target_idx = await self._judge(user_id, text, candidates)
                if verdict == "conflict" and target_idx is not None and 0 <= target_idx < len(candidates):
                    return "conflict", candidates[target_idx].id
                if verdict == "duplicate":
                    return "duplicate", None  # judge 可靠：判重就真的不写
                return "new", None  # judge 是最终裁决：判 new 就写新
            except Exception:  # noqa: BLE001 - degrade, never fail a write
                log.exception("judge stage failed; falling back to lexical")
        # 2. Lexical Jaccard: last resort (no judge, or judge failed).
        if await self._is_duplicate_lexical(user_id, text):
            return "duplicate", None
        return "new", None

    async def _judge(
        self, user_id: int, new_text: str, candidates: list[MemoryRow]
    ) -> tuple[str, int | None]:
        """Classify (new_text, candidates) via the injected judge, returning
        (verdict, target_index). A judge failure is fail-OPEN: treat as "new"
        (write it) - a dropped write is worse than an occasional duplicate.

        Candidates carry their ``created_at`` so the judge can tell OLD from NEW
        (a conflict is "same subject, newer value" — the timestamp makes that
        order explicit, e.g. 「9-01 记录:我住在杭州」 vs 「我搬到上海了」).
        """
        cand_texts = [f"{r.created_at[:10]} 记录：{r.text}" for r in candidates]
        try:
            return await self.judge(user_id, new_text, cand_texts)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - a broken judge must not fail a write
            log.exception("memory judge failed; treating as new (fail-open)")
            return "new", None

    async def _supersede(
        self, memory_id: int, user_id: int, text: str, vec: list[float] | None
    ) -> None:
        """Conflict resolution as fact succession: write the NEW value as a fresh
        row and retire the old one (``superseded_by`` → new id) instead of
        overwriting in place. History is retained (the old row stays, only
        filtered out of retrieval), so the past is answerable and a wrong
        overwrite is reversible by clearing ``superseded_by``.
        """
        async with AsyncSession(self.db.engine) as s:
            new_row = MemoryRow(user_id=user_id, text=text)
            s.add(new_row)
            await s.flush()  # capture the new id
            new_id = new_row.id
            old = await s.get(MemoryRow, memory_id)
            if old is not None and old.user_id == user_id:
                old.superseded_by = new_id
            await s.commit()
        await self._vector_add(new_id, user_id, text, vec)

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
        """Current (non-superseded) memories, newest first. Superseded rows are
        history — excluded from every retrieval surface so the agent only sees
        the latest truth, while the past stays in the table."""
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(
                    select(MemoryRow)
                    .where(
                        MemoryRow.user_id == user_id,
                        MemoryRow.superseded_by.is_(None),
                    )
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
        (stale Milvus entries), ids not owned by this user (safety), and
        SUPERSEDED rows (a retired value must never surface as a recall
        candidate, or the judge would re-conflict against dead history)."""
        async with AsyncSession(self.db.engine) as s:
            rows = (
                await s.execute(
                    select(MemoryRow).where(
                        MemoryRow.id.in_(ids),
                        MemoryRow.user_id == user_id,
                        MemoryRow.superseded_by.is_(None),
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
#: Character-level pattern for bigrams (spaces/punctuation dropped, so
#: "用户 0" and "用户0" produce the same bigrams).
_CHAR_RE = re.compile(r"[a-z0-9_]|[\u4e00-\u9fff]")


def _terms(s: str) -> set[str]:
    return set(_TERM_RE.findall(s.lower()))


def _terms_bi(s: str) -> set[str]:
    """unigrams + character bigrams (BM25 recall only).

    Bigrams are what make an ENTITY distinguishable: "用户0的主语言是Rust" and
    "用户1的主语言是Go" share every unigram of interest ("用","户","主","语","言")
    and differ only in a digit, so unigram IDF cannot tell them apart. The
    bigram "户0" / "户1" carries the entity and gets a high IDF.
    """
    low = s.lower()
    terms = set(_TERM_RE.findall(low))
    chars = _CHAR_RE.findall(low)
    terms.update(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return terms


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
