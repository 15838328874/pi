"""Alembic environment (async engine; URL from PI_DATABASE_URL)."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection

from pi.server.db import Base, engine_kwargs

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("PI_DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "PI_DATABASE_URL is not set. Migrations require an explicit database URL, "
            "e.g. mysql+aiomysql://user:pass@host:3306/pi_py"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    url = _database_url()
    engine = create_async_engine(url, pool_pre_ping=True, **engine_kwargs(url))
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
