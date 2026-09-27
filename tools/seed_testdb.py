"""Seed the pi_py_test schema with reusable fixture data.

Usage:
    set -a; . ./.env; . ./.env.test; set +a
    python tools/seed_testdb.py                      # idempotent, safe to re-run
    python tools/seed_testdb.py --reset              # wipe the schema, then seed
    python tools/seed_testdb.py --password 'x' --users 5

What it creates:
    admin             is_admin=1, promoted through UserRepo.set_admin (the DB-only path)
    alice/bob/carol   normal users, each with a populated session and an empty one
    overquota         1540 tokens used against a 1000 quota -> POST /runs returns 402
    disabled          is_active=0     -> login returns 401

Refuses to run unless PI_DATABASE_URL points at a *_test schema. Usage rows are
stamped with today's date and /v1/usage only reads the current month, so re-run
after a month rollover if you need quota data.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from pi.models import Message, Role, TextBlock, ToolCallBlock, ToolResultBlock
from pi.observability.metering import UsageTracker
from pi.server.auth import hash_password
from pi.server.config import ServerSettings
from pi.server.db import (
    Database,
    MessageRepo,
    MessageRow,
    SessionRepo,
    SessionRow,
    UsageRecord,
    User,
    UserRepo,
)

ADMIN = "admin"
NAMES = ("alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi")
# username -> column overrides + whether to create the session fixtures.
# 402 needs used >= quota, so overquota gets the normal fixtures (1540 tokens of
# usage) against a 1000-token quota. Note quota_tokens=0 would NOT block anyone:
# quota_check falls back to the default quota when it is not > 0.
EXTRA = (
    ("overquota", {"quota_tokens": 1000}, True),
    ("disabled", {"is_active": False}, False),
)
CONVERSATION = [
    (Role.user, [TextBlock(text="看下工作目录里都有什么文件？")]),
    (
        Role.assistant,
        [
            TextBlock(text="我列一下当前目录。"),
            ToolCallBlock(id="call_seed_1", name="ls", arguments='{"path":"."}'),
        ],
    ),
    (Role.user, [ToolResultBlock(tool_use_id="call_seed_1", content="README.md\nsrc/\ntests/\n")]),
    # the emoji is deliberate: 4-byte utf8mb4, proves the charset is not latin1
    (Role.assistant, [TextBlock(text="有一个 README.md 和 src/、tests/ 两个目录 ✅ 要读哪个？")]),
]


def _check_target(url: str) -> str:
    name = make_url(url).database or ""
    if not name.endswith("_test"):
        raise SystemExit(
            f"refusing to seed schema {name!r}: PI_DATABASE_URL must point at a *_test "
            "database (source .env.test first)"
        )
    return name


async def _reset(db: Database) -> None:
    async with AsyncSession(db.engine) as s:
        for model in (UsageRecord, MessageRow, SessionRow, User):  # FK order matters
            await s.execute(delete(model))
        await s.commit()


async def _counts(db: Database) -> dict[str, int]:
    tables = (
        ("users", User),
        ("sessions", SessionRow),
        ("messages", MessageRow),
        ("usage_records", UsageRecord),
    )
    async with AsyncSession(db.engine) as s:
        return {
            label: (await s.execute(select(func.count()).select_from(model))).scalar_one()
            for label, model in tables
        }


async def _seed_user(
    users: UserRepo,
    sessions: SessionRepo,
    messages: MessageRepo,
    tracker: UsageTracker,
    settings: ServerSettings,
    username: str,
    password: str,
    *,
    admin: bool = False,
    with_sessions: bool = True,
    overrides: dict | None = None,
) -> str:
    """Create one account plus its fixture data. Returns what actually happened."""
    overrides = overrides or {}
    note = "exists"
    user = await users.by_username(username)
    if user is None:
        user = await users.create(
            username,
            hash_password(password),
            is_admin=admin,
            quota_tokens=overrides.get("quota_tokens", settings.default_quota_tokens),
        )
        if not overrides.get("is_active", True):
            await users.set_active(user.id, False)
        note = "created"
    if admin and not user.is_admin:
        await users.set_admin(user.id, True)
        note += "+promoted"

    cwd = settings.workspace_root / username
    cwd.mkdir(parents=True, exist_ok=True)

    if with_sessions and not await sessions.list_for_user(user.id, limit=1):
        sid = (
            await sessions.create(user.id, "seeded conversation", settings.default_model, cwd)
        ).id
        await messages.append_many(
            sid,
            [
                {
                    "idx": i,
                    "role": role.value,
                    "blocks": Message(role=role, blocks=blocks).model_dump_json(),
                }
                for i, (role, blocks) in enumerate(CONVERSATION)
            ],
        )
        await tracker.record(
            user_id=user.id,
            username=username,
            session_id=sid,
            model=settings.default_model,
            input_tokens=1200,
            output_tokens=340,
            turns=2,
        )
        await sessions.create(user.id, "seeded empty", settings.default_model, cwd)
        note += "+sessions"
    return note


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--password", default="pi-test-123", help="password for every seeded account"
    )
    parser.add_argument("--users", type=int, default=3, help=f"normal users, max {len(NAMES)}")
    parser.add_argument(
        "--reset", action="store_true", help="delete every row first (test schema only)"
    )
    args = parser.parse_args()
    if not 1 <= args.users <= len(NAMES):
        raise SystemExit(f"--users must be between 1 and {len(NAMES)}")

    settings = ServerSettings.from_env()
    print(f"target schema: {_check_target(settings.database_url)}")

    db = Database(settings.database_url)
    users, sessions, messages = UserRepo(db), SessionRepo(db), MessageRepo(db)
    tracker = UsageTracker(db.engine, default_quota=settings.default_quota_tokens)

    async def seed(username: str, **kw) -> str:
        return await _seed_user(
            users, sessions, messages, tracker, settings, username, args.password, **kw
        )

    if args.reset:
        await _reset(db)
        print("reset: every row deleted")

    print(f"  {ADMIN:9s} {await seed(ADMIN, admin=True, with_sessions=False)}")
    for username in NAMES[: args.users]:
        print(f"  {username:9s} {await seed(username)}")
    for username, overrides, with_sessions in EXTRA:
        note = await seed(username, with_sessions=with_sessions, overrides=overrides)
        print(f"  {username:9s} {note} {overrides}")

    print("rows:", await _counts(db))
    print(f"login: any username above, password {args.password!r}")
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
