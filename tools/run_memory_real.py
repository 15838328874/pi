"""Runner for the REAL-STACK memory scale integration test.

Loads the project's .env.local (not .env — this repo keeps creds there), maps the
real creds into PI_ITEST_* (deliberately NOT the PI_* names, because tests/conftest.py
pins those to ""), points the infra vars at the LOCAL MySQL/Milvus, and invokes
pytest in-process so the API keys never touch the shell or stdout.

Usage:  python tools/run_memory_real.py
The integration test skips its rerank/judge phases when PI_ITEST_RERANK_* / PI_MODEL
are absent, so a partial run is still meaningful.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # repo root: makes `import integration.conftest` work
sys.path.insert(0, str(ROOT / "src"))


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main() -> int:
    env = _read_env(ROOT / ".env.local")
    for k, v in env.items():
        os.environ.setdefault(k, v)  # shell-set values win

    os.environ["PI_INTEGRATION"] = "1"

    # --- infra: LOCAL test DB (never the .env PI_DATABASE_URL's remote RDS) ---
    db_url = os.environ.get("PI_DATABASE_URL", "").strip()
    if db_url:
        from sqlalchemy.engine import make_url

        test_db_url = make_url(db_url).set(database="pi_py_test").render_as_string(
            hide_password=False
        )
    else:
        test_db_url = "mysql+aiomysql://pi:pi_py_local@127.0.0.1:3306/pi_py_test"
    os.environ.setdefault("PI_ITEST_DATABASE_URL", test_db_url)
    os.environ.setdefault("PI_ITEST_MILVUS_URI", os.environ.get("PI_MILVUS_URI", ""))

    # --- real creds: copy from the PI_* names loaded above ---------------------
    _map = {
        "PI_ITEST_EMBEDDING_URL": os.environ.get("PI_EMBEDDING_URL", ""),
        "PI_ITEST_EMBEDDING_API_KEY": os.environ.get("PI_EMBEDDING_API_KEY", ""),
        "PI_ITEST_EMBEDDING_MODEL": os.environ.get("PI_EMBEDDING_MODEL", ""),
        "PI_ITEST_RERANK_URL": os.environ.get("PI_RAG_RERANK_URL", ""),
        "PI_ITEST_RERANK_API_KEY": os.environ.get("PI_RAG_RERANK_API_KEY", ""),
        "PI_ITEST_RERANK_MODEL": os.environ.get("PI_RAG_RERANK_MODEL", ""),
    }
    for k, v in _map.items():
        if v:
            os.environ[k] = v

    # sanity report WITHOUT printing keys
    key_len = len(os.environ.get("PI_ITEST_EMBEDDING_API_KEY", ""))
    print(f"infra: MySQL={os.environ['PI_ITEST_DATABASE_URL'].split('@')[-1]} "
          f"Milvus={os.environ['PI_ITEST_MILVUS_URI']}")
    print(f"embedding: model={os.environ.get('PI_ITEST_EMBEDDING_MODEL')} api_key_len={key_len}")
    print(f"rerank: model={os.environ.get('PI_ITEST_RERANK_MODEL') or '(off)'} "
          f"url={'set' if os.environ.get('PI_ITEST_RERANK_URL') else '(off)'}")
    print(f"judge: PI_MODEL={os.environ.get('PI_MODEL') or '(off)'}")

    import pytest

    return pytest.main(["integration/test_memory_real_scale.py", "-q", "-s"])


if __name__ == "__main__":
    raise SystemExit(main())
