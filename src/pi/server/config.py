"""Production settings for the pi-py multi-user server (env-driven)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

# src/pi/server/config.py -> repo root in the checkout layout. Under a
# non-editable install this points somewhere with no web/dist, and the UI mount
# in app.py is skipped rather than failing.
_REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class ServerSettings:
    database_url: str = ""
    jwt_secret: str = ""
    token_ttl_minutes: int = 720
    default_model: str = ""
    workspace_root: Path = field(default_factory=lambda: Path.home() / ".pi-py" / "workspaces")
    max_concurrent_runs: int = 8
    run_timeout_seconds: int = 600
    rate_limit_runs_per_min: int = 20
    default_quota_tokens: int = 1_000_000
    tracer_backend: str = "jsonl"
    # OTLP/gRPC collector for PI_TRACER=otel (Jaeger, Tempo, an OTel Collector).
    # Empty falls through to the standard OTEL_EXPORTER_OTLP_* variables and then
    # to localhost:4317, so an already-instrumented deployment needs no pi-specific
    # variable at all.
    otlp_endpoint: str = ""
    # Fraction of runs traced. 1.0 keeps everything, which is what a self-hosted
    # collector wants; lower it when the volume, not the insight, is the problem.
    trace_sample_rate: float = 1.0
    # Resource attributes on every span: how the collector labels this service, and
    # which deployment it came from. Worth setting once per environment - "which of
    # the three installs is this trace from" is otherwise unanswerable.
    service_name: str = "pi-py"
    environment: str = ""
    # /metrics in Prometheus text format. On by default: it costs nothing until
    # something scrapes it, and a deployment nothing can scrape is a deployment
    # nothing can alert on.
    metrics_enabled: bool = True
    # Bearer token for /metrics. Empty leaves the endpoint open, which is right for
    # a compose network and wrong for a published port: the series are aggregate
    # (no usernames, no sessions, no prompts) but they do describe traffic volume
    # and which models are in use. Set it whenever the port is reachable.
    metrics_token: str = ""
    redis_url: str = ""
    redis_ns: str = "pi"
    sandbox: str = ""
    sandbox_image: str = "python:3.12-slim"
    sandbox_net: bool = False
    audit_path: Path = field(default_factory=lambda: Path.home() / ".pi-py" / "audit.jsonl")
    policy_path: str = ""
    # Reverse proxies whose X-Forwarded-For we trust. uvicorn's own default is
    # 127.0.0.1, which is wrong under compose: Caddy is a separate container with
    # a 172.x source IP, so the header gets dropped and every client looks like
    # Caddy. Comma-separated IPs or CIDRs. Never "*" — that lets clients spoof XFF.
    forwarded_allow_ips: str = "127.0.0.1"
    # Where this server is reachable from outside (scheme://host[:port]), used to
    # build the public download URLs for session uploads - the model gateway
    # fetches the file itself, so an empty value means file attachments are off.
    public_base_url: str = ""
    # Upload ceiling per file. The gateway reads the file into the model context,
    # so this bounds both disk and what one request can cost.
    max_upload_bytes: int = 20 * 1024 * 1024
    # Built frontend (web/ -> npm run build). Served by this process because
    # there is no CORS middleware: the UI and /v1 have to share an origin.
    # A missing directory just means "no UI here", which is not an error.
    web_dist: Path = field(default_factory=lambda: _REPO_ROOT / "web" / "dist")

    # --- long-term memory (extracted facts in Milvus) ---
    # Off unless BOTH milvus_uri and embedding_model are set. Deliberately no
    # startup validation: a deployment without a vector database is a normal
    # single-node install, not a misconfiguration.
    milvus_uri: str = ""
    milvus_token: str = ""
    milvus_ns: str = "pi"
    # Partition-key buckets. One partition per user is impossible at this scale, and
    # no partition key at all makes every search scan the whole collection; hashing
    # user_id into N buckets prunes each query to 1/N of it.
    milvus_partitions: int = 1024
    embedding_model: str = ""
    # 0 = the model's native width. MemoryService probes it before creating the
    # collection, so the schema and the vectors can never disagree.
    embedding_dim: int = 0
    # Model used for fact extraction. Left empty, the run's own model is reused -
    # correct but expensive; a cheap model here is the main cost lever.
    memory_model: str = ""
    memory_top_k: int = 5
    # How wide the vector stage recalls before the reranker narrows it. Clamped to at
    # least memory_top_k, since recalling fewer than we inject would make the second
    # stage pointless. This is not where the cost is: measured on the live endpoints,
    # 20 and 10 recalled the same facts and cost 626 vs 590 rerank tokens per turn,
    # because memory_min_similarity decides how many candidates ever reach rerank.
    memory_recall_k: int = 20
    # Cosine gate. With a reranker configured this is only a *recall* gate - it has to
    # avoid losing relevant facts, and the reranker does the rejecting. 0.35 is
    # measured, not round: it kept every relevant fact while off-topic queries
    # recalled nothing at all. 0.5 lost a relevant fact outright (a query about
    # testing conventions scores 0.447 against its own fact); 0.0 tripled the rerank
    # tokens and let an off-topic query through.
    memory_min_similarity: float = 0.35
    memory_dedup_similarity: float = 0.92
    memory_max_facts: int = 100
    memory_extract_min_chars: int = 400
    memory_extract_concurrency: int = 4
    memory_inject_max_chars: int = 2000
    # Second retrieval stage. Both empty (either one, really) leaves retrieval
    # single-stage, which still works and adds no round trip to the hot path.
    rerank_url: str = ""
    rerank_model: str = ""
    # Precision gate, in the reranker's own score range - not comparable to cosine.
    # 0.3 sits in a wide measured gap: off-topic queries scored at most 0.044, while
    # facts worth injecting scored 0.30 and up.
    memory_rerank_min_score: float = 0.3
    # Periodic contradiction arbitration. Empty model = off. Rarer and smarter
    # than extraction on purpose: the sweep pulls each dirty user's whole fact
    # list and asks one judgment call, so it wants the plus tier while extraction
    # runs the flash tier every run.
    memory_arbiter_model: str = ""
    memory_arbiter_interval_seconds: int = 900
    memory_arbiter_batch: int = 20
    # Facts unconfirmed for this many days stop being recalled (soft: the rows
    # stay in MySQL). 0 disables decay. The maintenance loop applies it.
    memory_decay_days: int = 90
    # Execution traces (agent_runs/agent_steps) older than this many days are
    # deleted, hourly, on run finalize. 0 keeps them forever.
    trace_retention_days: int = 30

    @classmethod
    def from_env(cls) -> "ServerSettings":
        base = Path.home() / ".pi-py"
        database_url = os.environ.get("PI_DATABASE_URL", "")
        if not database_url:
            raise RuntimeError(
                "PI_DATABASE_URL is not set. The server requires MySQL or Postgres, "
                "e.g. mysql+aiomysql://user:pass@host:3306/pi_py"
            )
        return cls(
            database_url=database_url,
            jwt_secret=os.environ.get("PI_JWT_SECRET") or _load_or_create_secret(base),
            token_ttl_minutes=int(os.environ.get("PI_TOKEN_TTL_MIN", 720)),
            default_model=os.environ.get("PI_MODEL", "openai/gpt-4o"),
            workspace_root=Path(os.environ.get("PI_WORKSPACE_ROOT", base / "workspaces")),
            max_concurrent_runs=int(os.environ.get("PI_MAX_CONCURRENT_RUNS", 8)),
            run_timeout_seconds=int(os.environ.get("PI_RUN_TIMEOUT_SECONDS", 600)),
            rate_limit_runs_per_min=int(os.environ.get("PI_RATE_LIMIT_RUNS_PER_MIN", 20)),
            default_quota_tokens=int(os.environ.get("PI_DEFAULT_QUOTA_TOKENS", 1_000_000)),
            tracer_backend=os.environ.get("PI_TRACER", "jsonl"),
            otlp_endpoint=os.environ.get("PI_OTLP_ENDPOINT", ""),
            trace_sample_rate=float(os.environ.get("PI_TRACE_SAMPLE_RATE", 1.0)),
            service_name=os.environ.get("PI_SERVICE_NAME", "pi-py"),
            environment=os.environ.get("PI_ENVIRONMENT", ""),
            metrics_enabled=os.environ.get("PI_METRICS", "1").strip().lower()
            not in ("0", "false", "no", "off", ""),
            metrics_token=os.environ.get("PI_METRICS_TOKEN", ""),
            redis_url=os.environ.get("PI_REDIS_URL", ""),
            redis_ns=os.environ.get("PI_REDIS_NS", "pi"),
            sandbox=os.environ.get("PI_SANDBOX", ""),
            sandbox_image=os.environ.get("PI_SANDBOX_IMAGE", "python:3.12-slim"),
            sandbox_net=os.environ.get("PI_SANDBOX_NET", "") == "host",
            audit_path=Path(os.environ.get("PI_AUDIT_PATH", base / "audit.jsonl")),
            policy_path=os.environ.get("PI_POLICY", ""),
            forwarded_allow_ips=os.environ.get("PI_FORWARDED_ALLOW_IPS", "127.0.0.1"),
            public_base_url=os.environ.get("PI_PUBLIC_BASE_URL", "").rstrip("/"),
            max_upload_bytes=int(os.environ.get("PI_MAX_UPLOAD_BYTES", 20 * 1024 * 1024)),
            web_dist=Path(os.environ.get("PI_WEB_DIST", "") or _REPO_ROOT / "web" / "dist"),
            milvus_uri=os.environ.get("PI_MILVUS_URI", ""),
            milvus_token=os.environ.get("PI_MILVUS_TOKEN", ""),
            milvus_ns=os.environ.get("PI_MILVUS_NS", "pi"),
            milvus_partitions=int(os.environ.get("PI_MILVUS_PARTITIONS", 1024)),
            embedding_model=os.environ.get("PI_EMBEDDING_MODEL", ""),
            embedding_dim=int(os.environ.get("PI_EMBEDDING_DIM", 0)),
            memory_model=os.environ.get("PI_MEMORY_MODEL", ""),
            memory_top_k=int(os.environ.get("PI_MEMORY_TOP_K", 5)),
            memory_recall_k=int(os.environ.get("PI_MEMORY_RECALL_K", 20)),
            memory_min_similarity=float(os.environ.get("PI_MEMORY_MIN_SIMILARITY", 0.35)),
            memory_dedup_similarity=float(os.environ.get("PI_MEMORY_DEDUP_SIMILARITY", 0.92)),
            memory_max_facts=int(os.environ.get("PI_MEMORY_MAX_FACTS", 100)),
            memory_extract_min_chars=int(os.environ.get("PI_MEMORY_EXTRACT_MIN_CHARS", 400)),
            memory_extract_concurrency=int(os.environ.get("PI_MEMORY_EXTRACT_CONCURRENCY", 4)),
            memory_inject_max_chars=int(os.environ.get("PI_MEMORY_INJECT_MAX_CHARS", 2000)),
            rerank_url=os.environ.get("PI_RERANK_URL", ""),
            rerank_model=os.environ.get("PI_RERANK_MODEL", ""),
            memory_rerank_min_score=float(os.environ.get("PI_MEMORY_RERANK_MIN_SCORE", 0.3)),
            memory_arbiter_model=os.environ.get("PI_MEMORY_ARBITER_MODEL", ""),
            memory_arbiter_interval_seconds=int(
                os.environ.get("PI_MEMORY_ARBITER_INTERVAL_SECONDS", 900)
            ),
            memory_arbiter_batch=int(os.environ.get("PI_MEMORY_ARBITER_BATCH", 20)),
            memory_decay_days=int(os.environ.get("PI_MEMORY_DECAY_DAYS", 90)),
            trace_retention_days=int(os.environ.get("PI_TRACE_RETENTION_DAYS", 30)),
        )


def _load_or_create_secret(base: Path) -> str:
    """Reuse a persisted dev secret; generate one on first boot."""
    secret_file = base / "jwt.secret"
    if secret_file.is_file():
        value = secret_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_hex(32)
    base.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(value, encoding="utf-8")
    return value
