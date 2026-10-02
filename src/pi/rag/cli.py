"""pi-py rag CLI: ingest / rebuild-index / eval / search.

Lives next to ``pi.evals.cli`` for the same reason: ``pi/cli.py`` stays a thin
argparse shell, and the heavy imports (sqlalchemy, pymilvus, parsers) only load
when a rag subcommand is actually invoked.

Subcommands map to the acceptance demo (对接文档 §10):
  ingest         load documents (a file, or every supported file in a directory)
  rebuild-index  re-derive the Milvus projection from the SQL source of truth
  eval           Recall@k / MRR report over a golden set
  search         one query with citations (proves the retrieval path end to end)

Every command prints what it did, including degradations. An ingest that landed
INDEX_PENDING (text stored, vectors failed) is reported as such rather than as a
success - the house rule is that附属系统失败要发噪音.

Note on --file-id: 对接文档 §4.9 describes ingesting from an already-uploaded
object (``ctx.store.get_bytes`` + ``FileRepo``). That file pipeline is NOT on
this branch (``ToolContext`` has no ``fs``/``files``/``store``), so per the doc's
own rule ("以代码为准") ingestion takes filesystem paths, matching §4.7's
``pi-py rag ingest --path <docs目录>``.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

# Suffixes the in-process parser layer actually handles. A directory ingest
# skips anything else LOUDLY (counted and reported) instead of silently
# ignoring it - an unsupported file that was meant to be indexed is a retrieval
# bug that looks like "the document doesn't say that".
SUPPORTED_SUFFIXES = (
    ".md", ".markdown", ".txt", ".pdf", ".docx", ".csv", ".tsv",
    ".html", ".htm", ".json", ".py", ".rst", ".xlsx",
)


def cmd_rag(args) -> int:
    sub = getattr(args, "rag_command", None)
    if sub == "ingest":
        return asyncio.run(_cmd_ingest(args))
    if sub == "rebuild-index":
        return asyncio.run(_cmd_rebuild(args))
    if sub == "eval":
        return asyncio.run(_cmd_eval(args))
    if sub == "search":
        return asyncio.run(_cmd_search(args))
    print("usage: pi-py rag {ingest,rebuild-index,eval,search} ...", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# shared runtime assembly
# ---------------------------------------------------------------------------


def _build(args, *, memory_vector: bool | None = None):
    """Assemble the runtime for a CLI invocation.

    The CLI allows the in-process vector store (unlike the server): a single
    process means an in-memory index is coherent for the lifetime of the
    command, and it lets `rag search`/`rag eval` work with zero Milvus.
    ``create_schema=True`` runs the idempotent DDL because a CLI invocation does
    NOT imply someone ran `pi-py migrate`.
    """
    import pi  # noqa: F401 - loads ./.env into os.environ (existing vars win)
    from pi.rag.adapters import build_runtime
    from pi.rag.config import RagConfig

    cfg = RagConfig.from_env()
    allow = memory_vector
    if allow is None:
        allow = not bool(cfg.milvus_uri)
    runtime = build_runtime(cfg, allow_memory_vector=allow, create_schema=True)
    if getattr(args, "verbose", False):
        print(
            f"[rag] store={runtime.backend} "
            f"vector={type(runtime.vector_store).__name__ if runtime.vector_store else 'off'} "
            f"embedder={type(runtime.embedder).__name__ if runtime.embedder else 'off'} "
            f"rerank={type(runtime.reranker).__name__ if runtime.reranker else 'off'}",
            file=sys.stderr,
        )
    return runtime


def _collect_files(args) -> list[Path]:
    """Resolve --path into a concrete file list (file, or supported files in a dir)."""
    raw = getattr(args, "path", "") or ""
    p = Path(raw).expanduser()
    if not p.exists():
        print(f"error: path not found: {p}", file=sys.stderr)
        return []
    if p.is_file():
        return [p]
    files = sorted(
        f for f in p.rglob("*")
        if f.is_file() and f.suffix.lower() in SUPPORTED_SUFFIXES
    )
    skipped = sorted(
        f for f in p.rglob("*")
        if f.is_file() and f.suffix.lower() not in SUPPORTED_SUFFIXES
    )
    if skipped:
        print(
            f"[rag] skipping {len(skipped)} unsupported file(s): "
            + ", ".join(f.name for f in skipped[:10])
            + (" ..." if len(skipped) > 10 else ""),
            file=sys.stderr,
        )
    return files


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


async def _cmd_ingest(args) -> int:
    from pi.rag.types import IngestStatus

    files = _collect_files(args)
    if not files:
        print("error: no ingestible files found", file=sys.stderr)
        return 1

    user_id = int(args.user)
    doc_id = getattr(args, "doc_id", "") or ""
    if doc_id and len(files) > 1:
        print(
            "error: --doc-id identifies ONE document; drop it to ingest a directory "
            "(each file is keyed by its absolute path)",
            file=sys.stderr,
        )
        return 1

    runtime = _build(args)
    ok = degraded = failed = 0
    total_tokens = 0
    try:
        for f in files:
            outcome = await runtime.ingest.ingest_file(
                f, user_id=user_id, doc_key=(doc_id or None)
            )
            total_tokens += outcome.usage_tokens
            mark = {
                IngestStatus.READY.value: "READY  ",
                IngestStatus.INDEX_PENDING.value: "INDEX!",
                IngestStatus.NEEDS_HEAVY_PARSER.value: "HEAVY  ",
                IngestStatus.FAILED.value: "FAILED ",
            }.get(outcome.status, outcome.status.upper()[:6].ljust(6))
            print(
                f"  {mark} {outcome.doc_key}\n"
                f"         title={outcome.title!r} stored={outcome.chunks_stored} "
                f"indexed={outcome.chunks_indexed} tokens={outcome.usage_tokens}"
            )
            if outcome.reason:
                print(f"         reason: {outcome.reason}")
            if outcome.status == IngestStatus.READY.value:
                ok += 1
            elif outcome.status in (
                IngestStatus.INDEX_PENDING.value, IngestStatus.NEEDS_HEAVY_PARSER.value
            ):
                degraded += 1
            else:
                failed += 1
    finally:
        await runtime.close()

    print(
        f"\n[rag] ingest: {len(files)} file(s) -> {ok} ready, {degraded} degraded, "
        f"{failed} failed; embedding tokens={total_tokens}"
    )
    if degraded:
        print(
            "[rag] degraded documents are stored in SQL but NOT vector-indexed; "
            "run `pi-py rag rebuild-index --user <id>` after fixing the cause "
            "(embedding endpoint down, or scanned PDF needing a heavy parser)."
        )
    if ok:
        # R1 propagation note. The vector channel (Milvus = shared state) sees
        # new chunks immediately in EVERY process. BM25 is cached per process,
        # and invalidate() only reaches the process that called it - so a
        # LONG-RUNNING server's shard was built before this CLI's writes. That
        # staleness is now BOUNDED by the TTL (RetrievalConfig.bm25_ttl_s,
        # PI_RAG_BM25_TTL, default 300s): each worker rebuilds its shard from
        # SQL truth on the next search after the shard expires, so it converges
        # without a restart. Restart / rebuild-index only buys IMMEDIACY.
        # Read the TTL from the runtime's own config (already assembled by
        # _build); close() releases backend connections but leaves the
        # dataclass readable. Do NOT call RagConfig.from_env() here - that name
        # is imported locally inside _build, not in this scope.
        ttl = runtime.config.retrieval.bm25_ttl_s
        if ttl > 0:
            print(
                f"[rag] note: the vector channel sees new chunks immediately; BM25 is "
                f"cached per process and other replicas pick it up within "
                f"~{ttl:g}s (PI_RAG_BM25_TTL). Restart the server or run "
                f"`pi-py rag rebuild-index --user <id>` only if you need it sooner."
            )
        else:
            print(
                "[rag] note: the vector channel sees new chunks immediately, but BM25 "
                "is cached per process and PI_RAG_BM25_TTL=0 (TTL off) - other replicas "
                "will NOT see it until you restart the server or run "
                "`pi-py rag rebuild-index --user <id>`."
            )
    return 1 if failed and not ok else 0


# ---------------------------------------------------------------------------
# rebuild-index
# ---------------------------------------------------------------------------


async def _cmd_rebuild(args) -> int:
    user_id = int(args.user)
    runtime = _build(args)
    try:
        report = await runtime.ingest.rebuild_index(user_id)
    finally:
        await runtime.close()

    total = int(report.get("total", 0) or 0)
    indexed = int(report.get("indexed", 0) or 0)
    status = str(report.get("status", "") or "")
    print(
        f"[rag] rebuild-index (user={user_id}): status={status} "
        f"indexed={indexed}/{total} tokens={report.get('usage_tokens', 0)}"
    )
    if status != "ok":
        # index_pending means the SQL truth is intact but the vector projection
        # is missing/partial. Say why, and say what still works - a silent
        # "rebuilt" here is how a dead embedding endpoint hides for weeks.
        print(f"[rag] WARNING: {status} - {report.get('reason', '')}")
        if total:
            print(
                f"[rag] only {indexed}/{total} chunks are vector-indexed; "
                "lexical (BM25) retrieval still works, semantic retrieval is partial."
            )
        return 1
    if total and indexed < total:
        print(f"[rag] WARNING: only {indexed}/{total} chunks were vector-indexed.")
        return 1
    if not total:
        print("[rag] nothing to rebuild: this user has no chunks in rag_chunks.")
    return 0


# ---------------------------------------------------------------------------
# eval
# ---------------------------------------------------------------------------


async def _cmd_eval(args) -> int:
    """Recall@k / MRR over a golden set, using the SHIPPED retriever.

    ``HybridRetriever.search_chunks`` already matches the harness's ``SearchFn``
    signature - that is deliberate, so the measured object and the served object
    are literally the same code path (no eval-only retrieval variant to drift).
    """
    from pi.rag.eval.harness import GoldenSet, rebind_golden
    from pi.rag.eval.runner import EvalRunner

    golden_path = Path(args.golden).expanduser()
    if not golden_path.is_file():
        print(f"error: golden set not found: {golden_path}", file=sys.stderr)
        return 1

    runtime = _build(args)
    try:
        golden = GoldenSet.load(golden_path)
        if getattr(args, "rebind", False):
            # Chunking config changes move doc_key#seq. Rebinding re-derives the
            # gold keys from the stored answer_excerpts instead of silently
            # scoring against keys that no longer exist (which reads as a
            # catastrophic recall collapse that is really a label artefact).
            #
            # The user comes from the golden set itself (each case carries
            # user_id), not from a CLI flag: rebind must read the SAME user's
            # chunks the cases were authored against, or every key looks moved.
            user_ids = {int(c.user_id) for c in golden.cases}
            if not user_ids:
                print("error: golden set has no cases; cannot rebind", file=sys.stderr)
                return 1
            if len(user_ids) > 1:
                print(
                    f"error: golden set spans {len(user_ids)} users {sorted(user_ids)}; "
                    "rebind needs exactly one (chunks are per-user)",
                    file=sys.stderr,
                )
                return 1
            chunks = await runtime.store.list_chunks_for_user(user_ids.pop())
            golden, report = rebind_golden(golden, chunks)
            print(report.markdown())
            if report.unresolved:
                print(
                    f"[rag] WARNING: {report.unresolved} case(s) could not be "
                    "rebound (excerpt gone from the corpus); they will score as misses."
                )

        runner = EvalRunner(runtime.store)
        eval_report = await runner.run(
            golden, runtime.retriever.search_chunks, config_name=args.config_name
        )
        print(eval_report.markdown())
    finally:
        await runtime.close()

    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = out if out.suffix else out / f"rag_eval_{stamp}.md"
        if target.is_dir():
            target = target / f"rag_eval_{stamp}.md"
        target.write_text(eval_report.markdown(), encoding="utf-8")
        print(f"[rag] report written: {target}")

    # Non-zero exit when retrieval is materially broken, so CI can gate on it.
    hit5 = eval_report.metrics.get("hit@5", 0.0)
    recall5 = eval_report.metrics.get("recall@5", 0.0)
    if eval_report.failed:
        print(f"[rag] {eval_report.failed} case(s) errored during retrieval")
        return 1
    if hit5 < float(getattr(args, "min_hit5", 0.0) or 0.0):
        print(f"[rag] hit@5={hit5:.3f} is below the required floor "
              f"(primary any-of metric)", file=sys.stderr)
        return 1
    if recall5 < float(getattr(args, "min_recall5", 0.0) or 0.0):
        print(f"[rag] recall@5={recall5:.3f} is below the required floor "
              f"(secondary all-of coverage metric)", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


async def _cmd_search(args) -> int:
    runtime = _build(args)
    try:
        result = await runtime.retriever.search(
            int(args.user), args.query, k=int(args.k or 0) or None
        )
    finally:
        await runtime.close()

    mode = getattr(result.mode, "value", str(result.mode))
    print(
        f"[rag] mode={mode} outcome={result.outcome} degraded={result.degraded} "
        f"{result.duration_ms}ms hits={len(result.chunks)}"
    )
    if result.degraded:
        print(
            "[rag] DEGRADED: the semantic vector channel was unavailable, so these "
            "results came from lexical/SQL matching. Paraphrased queries may miss."
        )
    if not result.chunks:
        print("(no passages found)")
        return 0
    for i, hit in enumerate(result.chunks, 1):
        where = hit.title_path or hit.title or "(untitled)"
        page = f" p.{hit.page}" if hit.page else ""
        origin = hit.source or hit.doc_key
        print(f"\n[{i}] {where}{page}  (score {hit.score:.4f})\n    {origin}")
        print(f"    {hit.text[:600]}")
    return 0
