"""Async persistence layer (SQLAlchemy 2.0): users, sessions, messages.

PI_DATABASE_URL selects the backend: mysql+aiomysql://... or
postgresql+asyncpg://... - the schema and queries are dialect-neutral.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
    select,
    update as sa_update,
    delete as sa_delete,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pi.memory.repo import MemoryRowIn, pack_embedding, unpack_embedding
from pi.memory.store import Fact


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: Clamp for the trace TEXT columns (prompt / args / detail). 200k chars is far
#: beyond any single tool result the tools layer produces (~20-50k), yet stays
#: clear of MEDIUMTEXT's byte ceiling even in worst-case 4-byte utf8mb4.
_TRACE_TEXT_MAX = 200_000


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
    # Plan.model_dump_json(), or NULL for "no plan yet". The only column here that
    # is nullable, and it has to be: MySQL rejects a literal DEFAULT on a TEXT
    # column (error 1101) while PostgreSQL rejects ADD COLUMN ... TEXT NOT NULL on
    # a table that already has rows unless a default is given. There is no shape
    # that satisfies both except NULL.
    plan: Mapped[str | None] = mapped_column(Text)


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


class UserMemory(Base):
    """Every extracted fact lives here first; Milvus only mirrors this table.

    The id is what the vector store uses as its primary key, so a MySQL row and
    its mirror can never drift apart into duplicate entries. `embedding` is the
    packed float32 vector (4KB at 1024 dims) - storing it here is what makes the
    index droppable: tools/rebuild_milvus.py recreates it with zero API calls.
    """

    __tablename__ = "user_memories"
    # The id doubles as the vector-store primary key, so it must never be
    # recycled: plain SQLite rowids restart at max(id)+1 after a delete, while
    # MySQL's auto_increment never runs backwards. AUTOINCREMENT closes that gap
    # and is a no-op on other dialects.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # Extraction clamps fact text to 200 chars (extract.MAX_TEXT_CHARS); the
    # column is 5x that so no path into this table can ever hit truncation.
    content: Mapped[str] = mapped_column(String(1024))
    kind: Mapped[str] = mapped_column(String(32), default="preference")
    source_session: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    last_seen_at: Mapped[str] = mapped_column(String(32), default=_now)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # False = the row changed (or Milvus was down) and the index is stale; the
    # maintenance loop retries these. Indexed because unsynced rows are the rare
    # case the pending sweep is hunting for.
    milvus_synced: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", index=True
    )
    embedding: Mapped[bytes] = mapped_column(LargeBinary)


class AuditEvent(Base):
    """Every audit record, MySQL-first: the admin endpoint queries this table
    and the JSONL file is a mirror. `actor` normalizes the user/username field
    the record dict carries, so tenant scoping - and deregistration erasure -
    is one indexed predicate instead of event-type gymnastics. `payload` is the
    record verbatim (the same JSON the mirror writes), which is why the admin
    API shape never needs a migration when a record gains a field."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(32), index=True)
    event: Mapped[str] = mapped_column(String(16), index=True)
    actor: Mapped[str] = mapped_column(String(64), default="", index=True)
    # tool_call events only; NULL for every other event type.
    tool: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)


class AgentRun(Base):
    """One POST /runs execution: the execution-trace record in MySQL.

    Carries `username` denormalized (usage_records-style) so the admin trace
    view needs no join, and so purge can erase by user_id while the list
    endpoint still shows a readable name. `flags` is the finalized anomaly
    verdict ("" when the run was unremarkable) - computed at write time so the
    admin anomaly filter is one predicate, not a scan."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    # Plain string, not FK: usage_records precedent. A trace must survive its
    # session row being deleted by other means, and purge erases both together.
    session_id: Mapped[str] = mapped_column(String(16), index=True)
    model: Mapped[str] = mapped_column(String(128))
    # The user's input for this run, verbatim - the trace is the troubleshooting
    # record, and "what did they ask" is the first question it has to answer.
    prompt: Mapped[str] = mapped_column(Text, default="")
    # Correlates the row with the JSON access log line (X-Request-Id), so an
    # IP/timestamp complaint and a failed run resolve to each other.
    request_id: Mapped[str] = mapped_column(String(16), default="")
    # Model-native capabilities this run enabled; a wrong-flag reproduction is
    # impossible without them.
    enable_search: Mapped[bool] = mapped_column(Boolean, default=False)
    builtin_tools: Mapped[str] = mapped_column(String(64), default="")
    # Message-index range this run wrote (inclusive). NULL when nothing was
    # persisted; otherwise the detail endpoint joins messages on it to replay
    # the full input -> tool -> output transcript.
    first_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ok | error | timeout
    status: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    error: Mapped[str] = mapped_column(String(256), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    failed_tools: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[float] = mapped_column(default=0.0)
    flags: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[str] = mapped_column(String(32), default=_now)
    ended_at: Mapped[str] = mapped_column(String(32), default="")


class AgentStep(Base):
    """One recorded step of a run: tool calls, plans, compactions, errors.

    Text deltas are deliberately absent - the run's first_idx/last_idx range
    already points at the full transcript in messages. `args` and `detail`
    carry the complete tool arguments and result (MEDIUMTEXT), not the 200-char
    preview the SSE stream shows: a trace exists to answer "what exactly did
    the tool get and return", and a preview cannot."""

    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    # tool_call | plan | compaction | error
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    # Full JSON arguments (tool_call steps) or empty.
    args: Mapped[str] = mapped_column(Text, default="")
    # Full result content / error message.
    detail: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[float] = mapped_column(default=0.0)
    ts: Mapped[str] = mapped_column(String(32), default=_now)


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


async def memory_dirty_users(
    engine: AsyncEngine, since: str, model: str, limit: int
) -> list[tuple[int, str]]:
    """Users whose stored facts changed at/after `since`, for the arbiter sweep.

    usage_records doubles as the change log: every record() pass writes a turns=0
    row carrying the extraction model, so "this user's facts changed" is already
    persisted and no extra dirty-table or Redis set is needed. Arbitration meters
    under a different model, which is what keeps a sweep from re-triggering
    itself. created_at is an indexed ISO-8601 VARCHAR, so the string comparison
    is chronological and rides the index.
    """
    stmt = (
        select(UsageRecord.user_id, UsageRecord.username)
        .where(
            UsageRecord.turns == 0,
            UsageRecord.model == model,
            UsageRecord.created_at >= since,
        )
        .distinct()
        .limit(limit)
    )
    async with AsyncSession(engine) as s:
        rows = (await s.execute(stmt)).all()
    return [(int(r[0]), str(r[1])) for r in rows]


class UserRepo:
    def __init__(self, db: Database):
        self.db = db

    async def count(self) -> int:
        async with AsyncSession(self.db.engine) as s:
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

    async def set_plan(self, session_id: str, plan_json: str) -> None:
        """Store the session's current plan (Plan.model_dump_json()).

        Last write wins; there is no plan history, because the submit_plan call
        that produced each plan is already an immutable ToolCallBlock in
        messages.blocks. The per-session run lock held by RunManager.run_turn is
        what makes the caller the only writer.
        """
        async with AsyncSession(self.db.engine) as s:
            await s.execute(
                sa_update(SessionRow).where(SessionRow.id == session_id).values(plan=plan_json)
            )
            await s.commit()

    async def for_user(self, user_id: int, session_id: str) -> SessionRow | None:
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(
                    select(SessionRow).where(
                        SessionRow.id == session_id, SessionRow.user_id == user_id
                    )
                )
            ).scalar_one_or_none()

    async def by_id(self, session_id: str) -> SessionRow | None:
        """Unscoped lookup for the public file route: ownership cannot be checked
        there (the model gateway fetches the URL with no token), so the session
        id itself is the bearer capability."""
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(select(SessionRow).where(SessionRow.id == session_id))
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

    async def list_for_session(self, session_id: str) -> Sequence[MessageRow]:
        async with AsyncSession(self.db.engine) as s:
            return (
                (
                    await s.execute(
                        select(MessageRow)
                        .where(MessageRow.session_id == session_id)
                        .order_by(MessageRow.idx)
                    )
                )
                .scalars()
                .all()
            )

    async def list_range(self, session_id: str, lo: int, hi: int) -> Sequence[MessageRow]:
        """Messages with lo <= idx <= hi: the transcript slice one run wrote,
        joined back by the trace detail endpoint through first_idx/last_idx."""
        async with AsyncSession(self.db.engine) as s:
            return (
                (
                    await s.execute(
                        select(MessageRow)
                        .where(
                            MessageRow.session_id == session_id,
                            MessageRow.idx >= lo,
                            MessageRow.idx <= hi,
                        )
                        .order_by(MessageRow.idx)
                    )
                )
                .scalars()
                .all()
            )

    async def count_for_session(self, session_id: str) -> int:
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(
                    select(func.count(MessageRow.id)).where(
                        MessageRow.session_id == session_id
                    )
                )
            ).scalar_one()


def _fact_of(row: UserMemory) -> Fact:
    return Fact(
        id=row.id,
        user_id=row.user_id,
        text=row.content,
        kind=row.kind,
        source_session=row.source_session,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
    )


def _fact_of_values(r: Any) -> Fact:
    """Same mapping for a column-selected row (get_by_ids skips the blob)."""
    return Fact(
        id=int(r[0]),
        user_id=int(r[1]),
        text=str(r[2]),
        kind=str(r[3]),
        source_session=str(r[4]),
        created_at=str(r[5]),
        last_seen_at=str(r[6]),
    )


class UserMemoryRepo:
    """MemoryRepo over user_memories. MySQL is the truth; Milvus mirrors it.

    Structurally identical to the other repos here (own AsyncSession per call,
    user_id in every predicate) so a fact that belongs to another user reads as
    absent - the same isolation contract VectorStore.delete has.
    """

    def __init__(self, db: Database):
        self.db = db

    async def insert_many(
        self, user_id: int, rows: Sequence[MemoryRowIn], *, synced: bool = False
    ) -> list[int]:
        if not rows:
            return []
        now = _now()
        async with AsyncSession(self.db.engine) as s:
            objs = [
                UserMemory(
                    user_id=int(user_id),
                    content=r.text,
                    kind=r.kind,
                    source_session=r.source_session,
                    created_at=r.created_at or now,
                    last_seen_at=now,
                    is_active=True,
                    milvus_synced=synced,
                    embedding=pack_embedding(r.embedding),
                )
                for r in rows
            ]
            s.add_all(objs)
            # flush assigns the autoincrement ids (commit alone would expire the
            # objects before their ids could be read back)
            await s.flush()
            ids = [o.id for o in objs]
            await s.commit()
        return ids

    async def touch(self, user_id: int, fact_id: int, embedding: Sequence[float]) -> bool:
        """One UPDATE, and its rowcount *is* the no-resurrection rule: a decayed
        or deleted row matches zero rows and returns False."""
        async with AsyncSession(self.db.engine) as s:
            res = await s.execute(
                sa_update(UserMemory)
                .where(
                    UserMemory.id == int(fact_id),
                    UserMemory.user_id == int(user_id),
                    UserMemory.is_active.is_(True),
                )
                .values(
                    last_seen_at=_now(),
                    embedding=pack_embedding(embedding),
                    milvus_synced=False,
                )
            )
            await s.commit()
            return bool(res.rowcount)

    async def get_active(
        self, user_id: int, limit: int = 500
    ) -> list[tuple[Fact, list[float]]]:
        stmt = (
            select(UserMemory)
            .where(UserMemory.user_id == int(user_id), UserMemory.is_active.is_(True))
            .order_by(UserMemory.created_at, UserMemory.id)
            .limit(limit)
        )
        async with AsyncSession(self.db.engine) as s:
            rows = (await s.execute(stmt)).scalars().all()
        return [(_fact_of(r), unpack_embedding(r.embedding)) for r in rows]

    async def get_by_ids(self, user_id: int, ids: Sequence[int]) -> list[Fact]:
        """Column-selected on purpose: the retrieval join runs every turn and
        has no use for the 4KB embedding blobs."""
        wanted = [int(i) for i in dict.fromkeys(ids)]
        if not wanted:
            return []
        stmt = select(
            UserMemory.id,
            UserMemory.user_id,
            UserMemory.content,
            UserMemory.kind,
            UserMemory.source_session,
            UserMemory.created_at,
            UserMemory.last_seen_at,
        ).where(
            UserMemory.user_id == int(user_id),
            UserMemory.id.in_(wanted),
            UserMemory.is_active.is_(True),
        )
        async with AsyncSession(self.db.engine) as s:
            rows = (await s.execute(stmt)).all()
        return [_fact_of_values(r) for r in rows]

    async def update_text(
        self,
        user_id: int,
        fact_id: int,
        text: str,
        kind: str,
        embedding: Sequence[float],
        source_session: str | None = None,
    ) -> bool:
        values: dict[str, Any] = {
            "content": text,
            "kind": kind,
            "last_seen_at": _now(),
            "embedding": pack_embedding(embedding),
            "milvus_synced": False,
        }
        if source_session is not None:
            values["source_session"] = source_session
        async with AsyncSession(self.db.engine) as s:
            res = await s.execute(
                sa_update(UserMemory)
                .where(
                    UserMemory.id == int(fact_id),
                    UserMemory.user_id == int(user_id),
                    UserMemory.is_active.is_(True),
                )
                .values(**values)
            )
            await s.commit()
            return bool(res.rowcount)

    async def count_active(self, user_id: int) -> int:
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(
                    select(func.count(UserMemory.id)).where(
                        UserMemory.user_id == int(user_id),
                        UserMemory.is_active.is_(True),
                    )
                )
            ).scalar_one()

    async def lru_ids(self, user_id: int, count: int) -> list[int]:
        stmt = (
            select(UserMemory.id)
            .where(UserMemory.user_id == int(user_id), UserMemory.is_active.is_(True))
            .order_by(UserMemory.last_seen_at, UserMemory.created_at, UserMemory.id)
            .limit(count)
        )
        async with AsyncSession(self.db.engine) as s:
            return [int(r) for r in (await s.execute(stmt)).scalars().all()]

    async def delete(self, user_id: int, fact_id: int) -> bool:
        async with AsyncSession(self.db.engine) as s:
            res = await s.execute(
                sa_delete(UserMemory).where(
                    UserMemory.id == int(fact_id),
                    UserMemory.user_id == int(user_id),
                )
            )
            await s.commit()
            return bool(res.rowcount)

    async def delete_user(self, user_id: int) -> int:
        async with AsyncSession(self.db.engine) as s:
            res = await s.execute(
                sa_delete(UserMemory).where(UserMemory.user_id == int(user_id))
            )
            await s.commit()
            return int(res.rowcount)

    async def deactivate_older_than(self, cutoff: str) -> list[tuple[int, int]]:
        """Decay pass: select-then-update so the caller learns which (user, fact)
        pairs to drop from the vector index. The UPDATE re-checks the predicate:
        a fact touched between the two statements must not be deactivated while
        its id is already in the returned pairs."""
        async with AsyncSession(self.db.engine) as s:
            pairs = (
                await s.execute(
                    select(UserMemory.user_id, UserMemory.id).where(
                        UserMemory.is_active.is_(True),
                        UserMemory.last_seen_at < cutoff,
                    )
                )
            ).all()
            if pairs:
                await s.execute(
                    sa_update(UserMemory)
                    .where(
                        UserMemory.id.in_([int(p[1]) for p in pairs]),
                        UserMemory.is_active.is_(True),
                        UserMemory.last_seen_at < cutoff,
                    )
                    .values(is_active=False)
                )
                await s.commit()
        return [(int(p[0]), int(p[1])) for p in pairs]

    async def pending_sync(self, limit: int) -> list[tuple[Fact, list[float]]]:
        stmt = (
            select(UserMemory)
            .where(UserMemory.is_active.is_(True), UserMemory.milvus_synced.is_(False))
            .order_by(UserMemory.id)
            .limit(limit)
        )
        async with AsyncSession(self.db.engine) as s:
            rows = (await s.execute(stmt)).scalars().all()
        return [(_fact_of(r), unpack_embedding(r.embedding)) for r in rows]

    async def mark_synced(self, ids: Sequence[int]) -> int:
        if not ids:
            return 0
        async with AsyncSession(self.db.engine) as s:
            res = await s.execute(
                sa_update(UserMemory)
                .where(UserMemory.id.in_([int(i) for i in ids]))
                .values(milvus_synced=True)
            )
            await s.commit()
            return int(res.rowcount)

    async def count_active_all(self) -> int:
        """Across every tenant - the rebuild tool's empty-source guard."""
        async with AsyncSession(self.db.engine) as s:
            return (
                await s.execute(
                    select(func.count(UserMemory.id)).where(
                        UserMemory.is_active.is_(True)
                    )
                )
            ).scalar_one()

    async def active_embedding_page(
        self, after_id: int, limit: int
    ) -> list[tuple[int, int, list[float]]]:
        """(fact_id, user_id, embedding) for active rows, id-ordered, keyset-paged.

        The rebuild tool walks these pages to mirror MySQL into a fresh index.
        Keyset (id > after) rather than OFFSET: no table scan per page, and the
        page boundary cannot skip or repeat a row while the walk runs.
        """
        stmt = (
            select(UserMemory.id, UserMemory.user_id, UserMemory.embedding)
            .where(UserMemory.is_active.is_(True), UserMemory.id > int(after_id))
            .order_by(UserMemory.id)
            .limit(limit)
        )
        async with AsyncSession(self.db.engine) as s:
            rows = (await s.execute(stmt)).all()
        return [
            (int(r.id), int(r.user_id), unpack_embedding(r.embedding)) for r in rows
        ]


class AuditEventRepo:
    """Batch writer + admin reader for audit_events. The drainer in
    AuditLogger owns the writes; the admin endpoint owns the reads."""

    def __init__(self, db: Database):
        self.db = db

    async def append_many(self, records: Sequence[dict[str, Any]]) -> int:
        """records: the exact dicts the JSONL mirror wrote (ts included)."""
        if not records:
            return 0
        async with AsyncSession(self.db.engine) as s:
            for rec in records:
                s.add(
                    AuditEvent(
                        ts=str(rec.get("ts", ""))[:32],
                        event=str(rec.get("event", ""))[:16],
                        actor=str(rec.get("user") or rec.get("username") or "")[:64],
                        tool=str(rec["tool"])[:64] if rec.get("tool") else None,
                        payload=json.dumps(rec, ensure_ascii=False, separators=(",", ":")),
                    )
                )
            await s.commit()
        return len(records)

    async def list_recent(
        self,
        *,
        event: str = "",
        actor: str = "",
        tool: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        stmt = select(AuditEvent).order_by(AuditEvent.id.desc())
        if event:
            stmt = stmt.where(AuditEvent.event == event[:16])
        if actor:
            stmt = stmt.where(AuditEvent.actor == actor[:64])
        if tool:
            stmt = stmt.where(AuditEvent.tool == tool[:64])
        async with AsyncSession(self.db.engine) as s:
            rows = (await s.execute(stmt.limit(min(int(limit), 500)))).scalars().all()
        return [json.loads(r.payload) for r in rows]


class AgentRunRepo:
    """Writer/reader for execution traces. run_turn buffers one run in memory
    (TraceRecorder in runner.py) and hands it to append() when the stream
    ends - one transaction for the run row and its steps, like message
    persistence, so a trace is all-or-nothing too."""

    def __init__(self, db: Database):
        self.db = db

    async def append(
        self,
        *,
        run_id: str,
        user_id: int,
        username: str,
        session_id: str,
        model: str,
        prompt: str = "",
        request_id: str = "",
        enable_search: bool = False,
        builtin_tools: Sequence[str] = (),
        first_idx: int | None = None,
        last_idx: int | None = None,
        status: str,
        error: str,
        input_tokens: int,
        output_tokens: int,
        turns: int,
        failed_tools: int,
        duration_ms: float,
        flags: Sequence[str],
        started_at: str,
        steps: Sequence[dict[str, Any]],
    ) -> int:
        async with AsyncSession(self.db.engine) as s:
            run = AgentRun(
                run_id=run_id[:16],
                user_id=int(user_id),
                username=username[:64],
                session_id=session_id[:16],
                model=model[:128],
                prompt=prompt[:_TRACE_TEXT_MAX],
                request_id=request_id[:16],
                enable_search=bool(enable_search),
                builtin_tools=",".join(t[:24] for t in builtin_tools)[:64],
                first_idx=None if first_idx is None else int(first_idx),
                last_idx=None if last_idx is None else int(last_idx),
                status=status[:16],
                error=error[:256],
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                turns=int(turns),
                failed_tools=int(failed_tools),
                duration_ms=float(duration_ms),
                flags=",".join(f[:16] for f in flags)[:64],
                started_at=started_at[:32],
                ended_at=_now(),
            )
            s.add(run)
            await s.flush()
            # Captured before commit: this bare AsyncSession expires instances on
            # commit, and touching run.id afterwards would do lazy IO outside
            # greenlet context (MissingGreenlet).
            run_pk = run.id
            for seq, step in enumerate(steps):
                s.add(
                    AgentStep(
                        run_id=run.id,
                        seq=seq,
                        kind=str(step.get("kind", ""))[:16],
                        name=str(step.get("name", ""))[:64],
                        ok=bool(step.get("ok", True)),
                        args=str(step.get("args", ""))[:_TRACE_TEXT_MAX],
                        detail=str(step.get("detail", ""))[:_TRACE_TEXT_MAX],
                        duration_ms=float(step.get("duration_ms", 0.0)),
                    )
                )
            await s.commit()
            return run_pk

    async def list_runs(
        self,
        *,
        username: str = "",
        session_id: str = "",
        status: str = "",
        anomalous: bool = False,
        limit: int = 50,
    ) -> list[AgentRun]:
        stmt = select(AgentRun).order_by(AgentRun.id.desc())
        if username:
            stmt = stmt.where(AgentRun.username == username[:64])
        if session_id:
            stmt = stmt.where(AgentRun.session_id == session_id[:16])
        if status:
            stmt = stmt.where(AgentRun.status == status[:16])
        if anomalous:
            stmt = stmt.where(AgentRun.flags != "")
        async with AsyncSession(self.db.engine) as s:
            return list(
                (await s.execute(stmt.limit(min(int(limit), 200)))).scalars().all()
            )

    async def get_run(self, run_id: str) -> tuple[AgentRun, list[AgentStep]] | None:
        async with AsyncSession(self.db.engine) as s:
            run = (
                (
                    await s.execute(
                        select(AgentRun).where(AgentRun.run_id == run_id[:16])
                    )
                )
                .scalars()
                .one_or_none()
            )
            if run is None:
                return None
            steps = (
                (
                    await s.execute(
                        select(AgentStep)
                        .where(AgentStep.run_id == run.id)
                        .order_by(AgentStep.seq)
                    )
                )
                .scalars()
                .all()
            )
            return run, list(steps)

    async def delete_older_than(self, cutoff: str) -> tuple[int, int]:
        """Retention pass: (runs, steps) removed. Steps first - they FK the
        runs being deleted."""
        async with AsyncSession(self.db.engine) as s:
            doomed = select(AgentRun.id).where(AgentRun.started_at < cutoff)
            steps = (
                await s.execute(
                    sa_delete(AgentStep).where(AgentStep.run_id.in_(doomed))
                )
            ).rowcount
            runs = (
                await s.execute(sa_delete(AgentRun).where(AgentRun.started_at < cutoff))
            ).rowcount
            await s.commit()
        return int(runs or 0), int(steps or 0)


async def purge_user(engine: AsyncEngine, user_id: int) -> dict[str, int]:
    """Erase every MySQL trace of one user. Deregistration / PIPL erasure.

    FK order matters: messages reference sessions, everything else references
    users; audit_events has no FK and is matched by username instead. One
    transaction so a failure leaves the account intact and the caller can
    retry - a half-erased user is the one shape this table set cannot be
    allowed to settle into. Returns per-table counts for the audit record.
    Vector-index cleanup is NOT here: MemoryService.clear owns that layer.
    """
    uid = int(user_id)
    async with AsyncSession(engine) as s:
        memories = (
            await s.execute(sa_delete(UserMemory).where(UserMemory.user_id == uid))
        ).rowcount
        # audit rows are keyed by username, not user_id - resolve it while the
        # users row still exists, inside the same transaction.
        audit_rows = (
            await s.execute(
                sa_delete(AuditEvent).where(
                    AuditEvent.actor
                    == select(User.username).where(User.id == uid).scalar_subquery()
                )
            )
        ).rowcount
        trace_steps = (
            await s.execute(
                sa_delete(AgentStep).where(
                    AgentStep.run_id.in_(
                        select(AgentRun.id).where(AgentRun.user_id == uid)
                    )
                )
            )
        ).rowcount
        trace_runs = (
            await s.execute(sa_delete(AgentRun).where(AgentRun.user_id == uid))
        ).rowcount
        messages = (
            await s.execute(
                sa_delete(MessageRow).where(
                    MessageRow.session_id.in_(
                        select(SessionRow.id).where(SessionRow.user_id == uid)
                    )
                )
            )
        ).rowcount
        sessions = (
            await s.execute(sa_delete(SessionRow).where(SessionRow.user_id == uid))
        ).rowcount
        usage = (
            await s.execute(sa_delete(UsageRecord).where(UsageRecord.user_id == uid))
        ).rowcount
        account = (
            await s.execute(sa_delete(User).where(User.id == uid))
        ).rowcount
        await s.commit()
    return {
        "memories": int(memories or 0),
        "audit_events": int(audit_rows or 0),
        "agent_steps": int(trace_steps or 0),
        "agent_runs": int(trace_runs or 0),
        "messages": int(messages or 0),
        "sessions": int(sessions or 0),
        "usage_records": int(usage or 0),
        "account": int(account or 0),
    }
