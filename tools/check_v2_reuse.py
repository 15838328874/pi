"""Pre-flight check for `ab_rag.py --corpus v2 --reuse`.

Verifies the corpus left behind by the previous A/B run (which ended with
--keep) is still present, so a --reuse run can skip the ~70min re-ingest:

  1. MySQL: rag_chunks / rag_docs rows for the v2 eval user, per-doc breakdown
  2. Milvus: the v2 itest collection exists and its row count matches SQL

Read-only. Prints no credentials. Exit code 0 = --reuse is safe.

Usage:
    .venv/Scripts/python.exe tools/check_v2_reuse.py [--corpus v2]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Local infra creds, same as integration/conftest.py documents.
_DB_URL = "mysql+aiomysql://pi:pi_py_local@127.0.0.1:3306/pi_py_test"
_MILVUS_URI = "http://127.0.0.1:19531"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="v2", choices=["v1", "v2"])
    args = ap.parse_args()

    from build_golden_set import select_corpus

    prof = select_corpus(args.corpus)
    user = prof["eval_user"]
    collection = prof["collection"]
    expected_docs = len(prof["corpus"])

    print(f"corpus={args.corpus} eval_user={user} collection={collection} "
          f"expected_docs={expected_docs}")

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_DB_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            n_chunks = (await conn.execute(
                text("SELECT COUNT(*) FROM rag_chunks WHERE user_id=:u"),
                {"u": user})).scalar()
            n_distinct = (await conn.execute(
                text("SELECT COUNT(DISTINCT doc_key) FROM rag_chunks "
                     "WHERE user_id=:u"), {"u": user})).scalar()
            n_docs = (await conn.execute(
                text("SELECT COUNT(*) FROM rag_docs WHERE user_id=:u"),
                {"u": user})).scalar()
            per_doc = (await conn.execute(
                text("SELECT doc_key, COUNT(*) c FROM rag_chunks "
                     "WHERE user_id=:u GROUP BY doc_key ORDER BY c DESC"),
                {"u": user})).all()
        print(f"SQL: rag_chunks={n_chunks} distinct_doc_key={n_distinct} "
              f"rag_docs={n_docs}")
        for key, cnt in per_doc:
            print(f"   {key:30} {cnt}")
    finally:
        await engine.dispose()

    from pymilvus import MilvusClient

    mc = MilvusClient(uri=_MILVUS_URI)
    try:
        cols = set(mc.list_collections())
        print(f"Milvus collections: {sorted(cols)}")
        if collection not in cols:
            print(f"FAIL: collection {collection} missing -> --reuse NOT safe")
            return 1
        stats = mc.get_collection_stats(collection)
        print(f"Milvus {collection} stats: {stats}")
    finally:
        mc.close()

    ok = (n_chunks > 0 and n_distinct == expected_docs and n_docs == expected_docs)
    if not ok:
        print(f"FAIL: SQL incomplete (want {expected_docs} docs, got "
              f"{n_distinct} distinct / {n_docs} rag_docs) -> --reuse NOT safe")
        return 1

    print(f"OK: {n_chunks} chunks across {n_distinct} docs -> --reuse is safe")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
