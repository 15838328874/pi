"""Detect (and repair) embedding-space drift in the Milvus projection.

Why this exists: the vector index is a REBUILDABLE PROJECTION of rag_chunks, but
nothing in the schema records WHICH embedding model produced a stored vector. So
the moment the configured model changes - a local 0.6B experiment, a vendor
model bump, a different dimension - the projection silently belongs to a
different vector space. Queries then embed with the NEW model and are compared
against OLD vectors: no error, no warning, just quietly worse retrieval. That is
exactly the failure this repo hit when an A/B run left 527 chunks embedded by a
local Qwen3-Embedding-0.6B while the deployment went back to the hosted model.

How the check works: self-retrieval. Embed a sample of chunks that are ALREADY in
the index and search for each with its own fresh vector. If the stored vectors
came from the same model the nearest neighbour is the chunk itself with cosine
~= 1.0; if the space drifted, the same chunk still tends to win (it is still the
nearest neighbour) but the cosine drops to 0.6-0.9 - which is the actual signal.
So the verdict is on the SCORE, not on the identity of the hit.

Usage (LOCAL infra by default - never touches .env's remote PI_DATABASE_URL):

    python tools/rebuild_eval_index.py --check            # report drift
    python tools/rebuild_eval_index.py --apply            # re-embed from SQL
    python tools/rebuild_eval_index.py --corpus v1 --check

Exit code is 1 when drift is detected (so CI/ops can gate on it), 0 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pi  # noqa: F401,E402  (loads ./.env into os.environ; existing vars win)

# Mirrors tools/build_golden_set.py::_PROFILES. Kept as a literal so this tool
# does not import the (heavy) golden-set builder just to read two ids.
PROFILES = {
    "v1": {"user": 992001, "collection": "pi_rag_chunks_itest"},
    "v2": {"user": 992002, "collection": "pi_rag_chunks_itest_v2"},
}

# LOCAL infra only: .env's PI_DATABASE_URL points at a remote RDS, and rebuilding
# the wrong database would be both useless and slow.
LOCAL_DB = "mysql+aiomysql://pi:pi_py_local@127.0.0.1:3306/pi_py_test"
LOCAL_MILVUS = "http://127.0.0.1:19531"

# Same model + same text => cosine ~1.0 (float noise only). Anything below this
# means the stored vectors were produced by a different embedding model.
MATCH_THRESHOLD = 0.98


async def _sample_self_cosines(embedder, vector_store, chunks, sample: int) -> list[tuple[float, str]]:
    """-> [(cosine, kind)] with kind in {"self", "other", "none"}.

    "none" (no hit at all) and "other"/low-cosine (hit, wrong space) are DIFFERENT
    diagnoses - an empty projection is not a drifted one - so they are kept apart
    here and classified in ``_verdict``.
    """
    step = max(1, len(chunks) // max(1, sample))
    picked = chunks[::step][:sample]
    res = await embedder.embed([c.text_to_embed() for c in picked])
    out: list[tuple[float, str]] = []
    for chunk, vec in zip(picked, res.vectors):
        hits = await vector_store.search(chunk.user_id, list(vec), 1)
        if not hits:
            print(f"    chunk {chunk.chunk_id}: NOT INDEXED (no vector in the collection)")
            out.append((0.0, "none"))
            continue
        top_id, score = hits[0]
        kind = "self" if int(top_id) == int(chunk.chunk_id) else f"other#{top_id}"
        print(f"    chunk {chunk.chunk_id}: top1={kind} cosine={score:.4f}")
        out.append((float(score), "self" if kind == "self" else "other"))
    return out


def _verdict(samples: list[tuple[float, str]]) -> tuple[bool, str]:
    if not samples:
        return False, "no chunks to sample - nothing in rag_chunks for this user"
    total = len(samples)
    missing = sum(1 for _, kind in samples if kind == "none")
    if missing == total:
        return True, (
            f"EMPTY PROJECTION: none of the {total} sampled chunks has a vector in the "
            "collection - it was never built or was dropped. This is NOT drift; run "
            "--apply to build it."
        )
    if missing:
        return True, (
            f"PARTIAL PROJECTION: {missing}/{total} sampled chunks have no vector - "
            "the rebuild was interrupted or a re-ingest left gaps. Run --apply"
        )
    mean = sum(c for c, _ in samples) / total
    if mean >= MATCH_THRESHOLD:
        return False, (
            f"MATCH: mean self-cosine {mean:.4f} >= {MATCH_THRESHOLD} - the stored "
            "vectors belong to the configured embedding model"
        )
    if mean < 0.5:
        return True, (
            f"DRIFT (severe): mean self-cosine {mean:.4f} - the projection is not "
            "merely stale, it is effectively a different vector space; run --apply"
        )
    return True, (
        f"DRIFT: mean self-cosine {mean:.4f} < {MATCH_THRESHOLD} - the vectors were "
        "produced by a DIFFERENT embedding model than the one now configured. "
        "Retrieval is silently degraded; run --apply"
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", default="v2", choices=sorted(PROFILES))
    ap.add_argument("--user", type=int, default=None, help="override the profile's user id")
    ap.add_argument("--collection", default=None, help="override the profile's collection")
    ap.add_argument("--sample", type=int, default=6, help="chunks to self-retrieve")
    ap.add_argument("--apply", action="store_true", help="re-embed + re-project from SQL")
    ap.add_argument("--check", action="store_true", help="report drift only (default)")
    args = ap.parse_args()

    prof = PROFILES[args.corpus]
    user_id = int(args.user if args.user is not None else prof["user"])
    collection = args.collection or prof["collection"]

    db_url = os.environ.get("PI_RAG_DATABASE_URL") or LOCAL_DB
    milvus_uri = os.environ.get("PI_RAG_MILVUS_URI") or LOCAL_MILVUS

    from pi.rag.config import RagConfig
    from pi.rag.defaults.http_embedder import HttpEmbedder
    from pi.rag.defaults.milvus_vector import MilvusRagVectorStore
    from pi.rag.defaults.mysql_store import MysqlChunkStore
    from pi.rag.ingest import IngestPipeline

    cfg = RagConfig.from_env()
    if not cfg.vector_enabled():
        print("ERROR: PI_EMBEDDING_URL / _API_KEY / _MODEL must be set in .env")
        return 2

    print(f"[drift-check] corpus={args.corpus} user={user_id} collection={collection}")
    print(f"  mysql  = {db_url.split('@')[-1]}")
    print(f"  milvus = {milvus_uri}")
    print(f"  model  = {cfg.embedding.model} (style={cfg.embedding.style}, "
          f"key_len={len(cfg.embedding.api_key)})")

    store = MysqlChunkStore(db_url, create_schema=False)
    vector_store = MilvusRagVectorStore(milvus_uri, collection=collection)
    embedder = HttpEmbedder(
        cfg.embedding.url, cfg.embedding.api_key, cfg.embedding.model,
        timeout=cfg.embedding.timeout_s, batch_size=cfg.embedding.batch_size,
        style=cfg.embedding.style, retries=cfg.embedding.retries,
        retry_backoff_s=cfg.embedding.retry_backoff_s,
    )

    try:
        chunks = await store.list_chunks_for_user(user_id)
        print(f"  SQL truth: {len(chunks)} chunk(s) for this user")
        if not chunks:
            print("  nothing to do: this user has no chunks in rag_chunks")
            return 0

        print("\n[before]")
        samples = await _sample_self_cosines(embedder, vector_store, chunks, args.sample)
        drifted, message = _verdict(samples)
        print(f"  -> {message}")

        if drifted and args.apply:
            print("\n[apply] re-embedding every chunk from the SQL truth ...")
            pipe = IngestPipeline(store, embedder, vector_store, config=cfg)
            report = await pipe.rebuild_index(user_id)
            print(f"  rebuild: status={report['status']} indexed={report['indexed']}/"
                  f"{report['total']} tokens={report['usage_tokens']}")
            if report["reason"]:
                print(f"  reason: {report['reason']}")

            print("\n[after]")
            samples = await _sample_self_cosines(embedder, vector_store, chunks, args.sample)
            drifted, message = _verdict(samples)
            print(f"  -> {message}")

        return 1 if drifted else 0
    finally:
        await embedder.aclose()
        await vector_store.close()
        await store.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
