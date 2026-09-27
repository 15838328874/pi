"""Production settings for the pi-py multi-user server (env-driven)."""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


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
    # Vector semantic memory (Milvus + cloud embedding). All four must be set
    # for the vector path to activate; any missing -> lexical retrieval only.
    embedding_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    milvus_uri: str = ""
    # MCP servers (JSON array string) and the skills root dir; empty = feature off.
    mcp_servers: list[dict] = field(default_factory=list)
    skills_dir: str = ""

    @property
    def vector_memory_enabled(self) -> bool:
        return all(
            (self.embedding_url, self.embedding_api_key, self.embedding_model, self.milvus_uri)
        )

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
            redis_url=os.environ.get("PI_REDIS_URL", ""),
            redis_ns=os.environ.get("PI_REDIS_NS", "pi"),
            sandbox=os.environ.get("PI_SANDBOX", ""),
            sandbox_image=os.environ.get("PI_SANDBOX_IMAGE", "python:3.12-slim"),
            sandbox_net=os.environ.get("PI_SANDBOX_NET", "") == "host",
            audit_path=Path(os.environ.get("PI_AUDIT_PATH", base / "audit.jsonl")),
            policy_path=os.environ.get("PI_POLICY", ""),
            forwarded_allow_ips=os.environ.get("PI_FORWARDED_ALLOW_IPS", "127.0.0.1"),
            embedding_url=os.environ.get("PI_EMBEDDING_URL", ""),
            embedding_api_key=os.environ.get("PI_EMBEDDING_API_KEY", ""),
            embedding_model=os.environ.get("PI_EMBEDDING_MODEL", ""),
            milvus_uri=os.environ.get("PI_MILVUS_URI", ""),
            mcp_servers=_parse_mcp_servers(os.environ.get("PI_MCP_SERVERS", "")),
            skills_dir=os.environ.get("PI_SKILLS_DIR", ""),
        )


def _parse_mcp_servers(raw: str) -> list[dict]:
    """PI_MCP_SERVERS JSON array; malformed JSON warns and yields no servers
    (MCP is an optional feature - a typo must not take the server down)."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logging.getLogger("pi.server.config").warning(
            "PI_MCP_SERVERS is not valid JSON (%s); MCP disabled", exc
        )
        return []
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        logging.getLogger("pi.server.config").warning(
            "PI_MCP_SERVERS must be a JSON array of objects; MCP disabled"
        )
        return []
    return data


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
