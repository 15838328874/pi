"""pi-py command line interface (server deployment).

Subcommands:
- pi-py serve     run the multi-user HTTP server (FastAPI + JWT + SSE)
- pi-py migrate   apply database migrations (alembic upgrade head)
"""

from __future__ import annotations

import argparse
import sys

from pi import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-py",
        description="pi-py: Python implementation of the pi coding agent",
    )
    parser.add_argument("--version", action="version", version=f"pi-py {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the multi-user HTTP server (FastAPI + JWT)")
    serve.add_argument("--host", default="127.0.0.1", help="bind host (default: %(default)s)")
    serve.add_argument("--port", type=int, default=8300, help="bind port (default: %(default)s)")
    serve.add_argument("--db", default=None, help="database URL (default: $PI_DATABASE_URL)")

    migrate = sub.add_parser("migrate", help="apply database migrations (alembic upgrade head)")
    migrate.add_argument("--db", default=None, help="database URL (default: $PI_DATABASE_URL)")

    ev = sub.add_parser("eval", help="run agent eval tasks (run / diff / rollout)")
    ev_sub = ev.add_subparsers(dest="eval_command")
    ev_run = ev_sub.add_parser("run", help="run a task set and print a report")
    ev_run.add_argument("--tasks", required=True, help="task file or directory of *.json")
    ev_run.add_argument("--model", default=None, help="model (provider/model)")
    ev_diff = ev_sub.add_parser("diff", help="A/B two models on the same task set")
    ev_diff.add_argument("--tasks", required=True, help="task file or directory of *.json")
    ev_diff.add_argument("--model-a", required=True, help="baseline model")
    ev_diff.add_argument("--model-b", required=True, help="candidate model")
    ev_rl = ev_sub.add_parser(
        "rollout", help="batch rollout for the RL data flywheel (sft/rlvr JSONL export)"
    )
    ev_rl.add_argument("--tasks", required=True, help="task file or directory of *.json")
    ev_rl.add_argument("--model", required=True, help="rollout model (provider/model)")
    ev_rl.add_argument("--n", type=int, default=8, help="samples per task (GRPO group size)")
    ev_rl.add_argument("--concurrency", type=int, default=16, help="parallel rollouts")
    ev_rl.add_argument("--out", default="data/rl", help="output directory for the JSONL files")
    ev_rl.add_argument("--sandbox", default="", help="execution sandbox: '' (host) or 'docker'")
    ev_rl.add_argument("--judge-model", default=None, help="judge scorer model override")
    ev_rl.add_argument("--no-filter", action="store_true", help="skip rejection sampling")
    ev_rl.add_argument("--no-partial-credit", action="store_true", help="binary 0/1 rewards")

    _add_rag_subparsers(sub)

    return parser


def _add_rag_subparsers(sub) -> None:
    """pi-py rag {ingest,rebuild-index,eval,search} (对接文档 §4.7).

    Heavy imports stay inside pi.rag.cli (loaded on dispatch), so argparse builds
    fast and a missing pymilvus/sqlalchemy only bites when rag is actually used.
    """
    rag = sub.add_parser("rag", help="enterprise document RAG (ingest / index / eval / search)")
    rag.add_argument("--db", default=None, help="database URL (default: $PI_DATABASE_URL)")
    rag.add_argument("-v", "--verbose", action="store_true", help="print the resolved backends")
    rag_sub = rag.add_subparsers(dest="rag_command")

    ing = rag_sub.add_parser("ingest", help="parse + chunk + embed + index documents")
    ing.add_argument("--path", required=True, help="a file, or a directory to walk recursively")
    ing.add_argument("--user", required=True, type=int, help="integer user id that owns the docs (ACL)")
    ing.add_argument("--doc-id", default="", help="stable doc_key for a SINGLE file (default: abs path)")

    rb = rag_sub.add_parser("rebuild-index", help="re-derive the Milvus projection from SQL truth")
    rb.add_argument("--user", required=True, type=int, help="integer user id to rebuild")

    ev = rag_sub.add_parser("eval", help="Recall@k / MRR over a golden set (shipped retriever)")
    ev.add_argument("--golden", required=True, help="golden set JSON (evals/tasks/rag/*.json)")
    ev.add_argument("--config-name", default="shipped", help="label for the report")
    ev.add_argument("--out", default="", help="write the markdown report here (file or dir)")
    ev.add_argument("--rebind", action="store_true",
                    help="re-anchor gold keys to current chunking via answer_excerpts")
    ev.add_argument("--min-hit5", default="0.0",
                    help="exit non-zero if hit@5 (PRIMARY metric, any-of) is below "
                         "this floor. Prefer this over --min-recall5: hit@k asks "
                         "'was the answer found', recall@k additionally asks 'were "
                         "ALL relevant chunks found' and is capped by gold size")
    ev.add_argument("--min-recall5", default="0.0",
                    help="exit non-zero if recall@5 is below this floor (CI gate; "
                         "secondary all-of coverage metric)")

    se = rag_sub.add_parser("search", help="one cited retrieval (proves the path end to end)")
    se.add_argument("--query", required=True, help="the question")
    se.add_argument("--user", required=True, type=int, help="integer user id (ACL)")
    se.add_argument("--k", type=int, default=0, help="max passages (default: config final_k)")


def cmd_migrate(args: argparse.Namespace) -> int:
    import os
    import subprocess
    from pathlib import Path

    if args.db:
        os.environ["PI_DATABASE_URL"] = args.db
    # Locate alembic.ini: explicit PI_ALEMBIC_DIR (containers bake it into
    # the image), else fall back to the repo checkout layout (src/pi/cli.py).
    repo_root = None
    candidates = [os.environ.get("PI_ALEMBIC_DIR"), str(Path(__file__).resolve().parents[2])]
    for candidate in candidates:
        if candidate and (Path(candidate) / "alembic.ini").is_file():
            repo_root = Path(candidate)
            break
    if repo_root is None:
        print(
            "error: alembic.ini not found; set PI_ALEMBIC_DIR or run from a repo checkout",
            file=sys.stderr,
        )
        return 1
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(repo_root),
        env=os.environ.copy(),
    )
    return result.returncode


def cmd_serve(args: argparse.Namespace) -> int:
    import os

    import uvicorn

    from pi.server import create_app
    from pi.server.config import ServerSettings

    if args.db:
        os.environ["PI_DATABASE_URL"] = args.db
    settings = ServerSettings.from_env()
    print(f"trusted proxies for X-Forwarded-For: {settings.forwarded_allow_ips}", flush=True)
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        log_level="info",
        forwarded_allow_ips=settings.forwarded_allow_ips,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "migrate":
        return cmd_migrate(args)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "eval":
        from pi.evals.cli import cmd_eval

        return cmd_eval(args)
    if args.command == "rag":
        import os

        from pi.rag.cli import cmd_rag

        # An explicit --db wins over .env: `pi` was imported at module top (so
        # .env is already in os.environ), and this assignment overwrites it. The
        # rag CLI reads PI_DATABASE_URL when it assembles the runtime below.
        if getattr(args, "db", None):
            os.environ["PI_DATABASE_URL"] = args.db
        return cmd_rag(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
