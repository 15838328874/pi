"""Real-stack integration tests (MySQL + Redis + Milvus + cloud embedding).

These never run by default: the unit suite must stay offline. Enable with
PI_INTEGRATION=1 plus the explicit PI_ITEST_* variables - deliberately NOT the
PI_* names, because tests/conftest.py pins those to "" before pi is imported
and an integration run would otherwise read the pins instead of real values.

    PI_INTEGRATION=1 \
    PI_ITEST_DATABASE_URL=mysql+aiomysql://pi:pi_py_local@127.0.0.1:3306/pi_py_test \
    PI_ITEST_REDIS_URL=redis://127.0.0.1:6379/0 \
    PI_ITEST_MILVUS_URI=http://127.0.0.1:19531 \
    PI_ITEST_EMBEDDING_URL=https://.../api/v1/services/embeddings/text-embedding/text-embedding \
    PI_ITEST_EMBEDDING_API_KEY=sk-... \
    PI_ITEST_EMBEDDING_MODEL=qwen3.7-text-embedding \
    pytest integration/ -q
"""

import os

import pytest


def require_embedding_vars() -> None:
    missing = [
        k
        for k in (
            "PI_ITEST_MILVUS_URI",
            "PI_ITEST_EMBEDDING_URL",
            "PI_ITEST_EMBEDDING_API_KEY",
            "PI_ITEST_EMBEDDING_MODEL",
        )
        if not os.environ.get(k)
    ]
    if missing:
        pytest.skip(f"missing real-stack env: {', '.join(missing)}")
