"""Async persistence layer (SQLAlchemy 2.0): users, sessions, messages.

PI_DATABASE_URL selects the backend: mysql+aiomysql://... or
postgresql+asyncpg://... - the schema and queries are dialect-neutral.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, delete as sa_delete, func, select, update as sa_update
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

    async def create(self, user_id: int, title: str, model: str, cwd: Path) -> SessionRow:
        row = SessionRow(
            id=uuid.uuid4().hex[:12],
            user_id=user_id,
            title=title[:128],
            model=model,
            cwd=str(cwd),
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
    ):
        self.db = db
        self.vector_store = vector_store
        self.embedder = embedder
        # (user_id, tokens) after a successful embed call - the app wires this
        # to the usage tracker so embedding spend is metered like LLM tokens.
        self.on_embed_usage = on_embed_usage
        # (outcome, duration_s) per search - the app wires this to metrics so
        # a retrieval served by the lexical fallback (index down) makes noise.
        self.on_retrieval = on_retrieval

    async def add(self, user_id: int, text: str) -> None:
        async with AsyncSession(self.db.engine) as s:
            row = MemoryRow(user_id=user_id, text=text)
            s.add(row)
            await s.flush()  # capture the autoincrement id for the vector key
            memory_id = row.id
            await s.commit()
        await self._vector_add(memory_id, user_id, text)

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
        ids, failure = await self._vector_search(user_id, query, k)
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

    async def _vector_add(self, memory_id: int, user_id: int, text: str) -> None:
        """Best-effort vector upsert; failures are logged and swallowed so a
        memory write can never fail a run (the row is already committed)."""
        if self.embedder is None or self.vector_store is None:
            return
        try:
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
    ) -> tuple[list[int] | None, str | None]:
        """(ids, failure). ids None on failure or empty result -> the caller
        falls back to lexical. failure distinguishes the two outage kinds:
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
            ids = await self.vector_store.search(user_id, vec, k)
        except Exception:  # noqa: BLE001 - memory must never fail a run
            log.exception("vector memory search failed (index); falling back to lexical")
            return None, "store_failed"
        return ids or None, None

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
