"""Rebuild the Milvus memory index from MySQL, which owns every fact.

Usage:
    set -a; . ./.env; set +a
    python tools/rebuild_milvus.py --yes           # drop + recreate + bulk upsert
    python tools/rebuild_milvus.py --yes --batch 500

Run it with the server stopped or quiet: a fact touched mid-walk gets a stale
vector until its next touch (the maintenance loop re-syncs nothing it believes
synced), and a fact deleted mid-walk leaves a zombie that the repo join already
filters out on read.

What it does, in order:
  1. guards: refuses the production namespace "pi" without --yes, and refuses
     to drop anything when MySQL holds zero active facts - an empty source with
     a populated index almost always means PI_DATABASE_URL points at the wrong
     schema, and wiping the index would turn a config typo into an outage.
  2. drops {PI_MILVUS_NS}_memories and recreates it: deterministic-PK schema,
     same dim, so upserts stay idempotent afterwards.
  3. walks active rows in id-ordered pages and bulk-upserts the embedding
     blobs stored in MySQL - zero embedding/LLM API calls, zero cost.
  4. marks exactly the upserted ids milvus_synced=1, so the maintenance loop
     does not redo the walk.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi.memory.store import MilvusStore
from pi.server.config import ServerSettings
from pi.server.db import Database, UserMemoryRepo

PROD_NS = "pi"


async def _rebuild(settings: ServerSettings, batch: int) -> int:
    db = Database(settings.database_url)
    await db.init()
    repo = UserMemoryRepo(db)
    try:
        active = await repo.count_active_all()
        if active == 0:
            raise SystemExit(
                "MySQL has zero active memories; refusing to drop the index.\n"
                "  Most likely PI_DATABASE_URL points at the wrong schema - check it\n"
                "  against the server's .env before rebuilding anything."
            )
        # Constructed directly, not via get_store(): an operator tool wants the
        # loud failure, not the NoOp degradation the service boots with.
        store = MilvusStore(
            settings.milvus_uri,
            settings.milvus_token,
            namespace=settings.milvus_ns,
            dim=settings.embedding_dim or 512,
            num_partitions=settings.milvus_partitions,
        )
        try:
            dropped = await store.drop()
            await store.setup(settings.embedding_dim or 512)

            started = time.monotonic()
            total = 0
            after_id = 0
            while True:
                page = await repo.active_embedding_page(after_id, batch)
                if not page:
                    break
                by_user: dict[int, list[tuple[int, list[float]]]] = defaultdict(list)
                for fid, uid, vec in page:
                    by_user[uid].append((fid, vec))
                for uid, rows in by_user.items():
                    await store.upsert(uid, rows)
                # Exactly what was upserted - not "everything active": a row
                # touched while its page was in flight keeps its pending flag.
                await repo.mark_synced([fid for fid, _, _ in page])
                total += len(page)
                after_id = page[-1][0]
                print(f"  {total}/{active} fact(s) mirrored")
            print(
                f"rebuilt {store.collection}: {total} fact(s) "
                f"({'dropped and recreated' if dropped else 'created fresh'}, "
                f"{time.monotonic() - started:.1f}s)"
            )
            return total
        finally:
            await store.close()
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--yes",
        action="store_true",
        help=f"really drop and rebuild the '{PROD_NS}_memories' collection",
    )
    parser.add_argument(
        "--batch", type=int, default=500, help="rows per MySQL page / Milvus upsert"
    )
    args = parser.parse_args()
    if not 1 <= args.batch <= 5000:
        raise SystemExit("--batch must be between 1 and 5000")

    if not os.environ.get("PI_DATABASE_URL"):
        raise SystemExit("PI_DATABASE_URL is not set - source .env first")
    settings = ServerSettings.from_env()
    if not settings.milvus_uri:
        raise SystemExit("PI_MILVUS_URI is empty - nothing to rebuild against.")
    if settings.milvus_ns == PROD_NS and not args.yes:
        raise SystemExit(
            f"refusing to touch namespace '{PROD_NS}' (production) without --yes.\n"
            f"  This drops {PROD_NS}_memories and rebuilds it from MySQL. The index\n"
            f"  is disposable, but retrieval is offline until the walk finishes."
        )

    total = asyncio.run(_rebuild(settings, args.batch))
    print(f"done: {total} fact(s) now mirrored from MySQL")


if __name__ == "__main__":
    main()
