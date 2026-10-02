"""Runner for the REAL-STACK RAG integration test (local MySQL + Milvus + real model).

Why this exists: integration/test_rag_real.py reads PI_ITEST_* (deliberately not
PI_* names, because tests/conftest.py pins those to ""). The real embedding
creds live in pi-dev/.env under PI_EMBEDDING_*. This runner loads .env, maps
the creds into PI_ITEST_*, points the infra vars at the LOCAL MySQL/Milvus, and
invokes pytest in-process - so the API key never touches the shell or stdout.

Usage:  python tools/run_rag_real.py
Override infra via env: PI_ITEST_DATABASE_URL / PI_ITEST_MILVUS_URI if needed.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # repo root: makes `import integration.conftest` work
sys.path.insert(0, str(ROOT / "src"))

import pi  # noqa: F401,E402  (loads ./.env into os.environ; existing vars win)

# --- infra: LOCAL only (never the remote RDS in .env's PI_DATABASE_URL) ------
os.environ["PI_INTEGRATION"] = "1"
os.environ.setdefault(
    "PI_ITEST_DATABASE_URL",
    "mysql+aiomysql://pi:pi_py_local@127.0.0.1:3306/pi_py_test",
)
os.environ.setdefault("PI_ITEST_MILVUS_URI", "http://127.0.0.1:19531")

# --- real embedding creds: copy from the .env-loaded PI_EMBEDDING_* ----------
_map = {
    "PI_ITEST_EMBEDDING_URL": os.environ.get("PI_EMBEDDING_URL", ""),
    "PI_ITEST_EMBEDDING_API_KEY": os.environ.get("PI_EMBEDDING_API_KEY", ""),
    "PI_ITEST_EMBEDDING_MODEL": os.environ.get("PI_EMBEDDING_MODEL", ""),
}
# --- real rerank creds: copy from the .env-loaded PI_RAG_RERANK_* ------------
# Optional: the retrieval test skips its rerank phase when these are absent.
_map.update({
    "PI_ITEST_RERANK_URL": os.environ.get("PI_RAG_RERANK_URL", ""),
    "PI_ITEST_RERANK_API_KEY": os.environ.get("PI_RAG_RERANK_API_KEY", ""),
    "PI_ITEST_RERANK_MODEL": os.environ.get("PI_RAG_RERANK_MODEL", ""),
})
for k, v in _map.items():
    if v:
        os.environ[k] = v

# sanity: report readiness WITHOUT printing the key
missing = [k for k, v in _map.items() if not v and k not in
           ("PI_ITEST_RERANK_URL",
            "PI_ITEST_RERANK_API_KEY", "PI_ITEST_RERANK_MODEL")]
key_len = len(os.environ.get("PI_ITEST_EMBEDDING_API_KEY", ""))
print(f"infra: MySQL={os.environ['PI_ITEST_DATABASE_URL'].split('@')[-1]} "
      f"Milvus={os.environ['PI_ITEST_MILVUS_URI']}")
print(f"embedding: model={os.environ.get('PI_ITEST_EMBEDDING_MODEL')} "
      f"api_key_len={key_len}")
print(f"rerank: model={os.environ.get('PI_ITEST_RERANK_MODEL') or '(off)'} "
      f"url={'set' if os.environ.get('PI_ITEST_RERANK_URL') else '(off)'}")
if missing:
    print(f"MISSING (test will skip): {missing}")

import pytest  # noqa: E402

sys.exit(pytest.main([
    "integration/test_rag_real.py",
    "integration/test_rag_retrieval_real.py",
    "-q", "-s",
]))
