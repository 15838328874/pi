#!/usr/bin/env python3
"""Dump the server's OpenAPI schema to web/openapi.json for frontend codegen.

Builds the FastAPI app against a throwaway SQLite file with every external
dependency pinned off, so this never touches a production database, Redis, or
the sandbox. Re-run it after changing a route or a request/response model, then
`npm run gen:api` inside web/ to regenerate the TypeScript types.

The dump is committed on purpose: it makes the front/back contract reviewable in
a diff, and lets codegen run offline.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

# Must be set before `import pi`: pi/__init__.py loads ./.env on import, and
# existing environment variables win, so pinning here is enough to stay isolated
# from whatever the repo-root .env says.
_tmp = Path(tempfile.mkdtemp(prefix="pi-openapi-"))
os.environ["PI_DATABASE_URL"] = f"sqlite+aiosqlite:///{(_tmp / 'schema.db').as_posix()}"
os.environ["PI_REDIS_URL"] = ""
os.environ["PI_SANDBOX"] = ""
os.environ["PI_POLICY"] = ""
os.environ["PI_TRACER"] = "noop"
os.environ["PI_JWT_SECRET"] = "schema-dump-only-not-a-real-secret"
os.environ["PI_WORKSPACE_ROOT"] = str(_tmp / "ws")
os.environ["PI_AUDIT_PATH"] = str(_tmp / "audit.jsonl")
os.environ["PI_MODEL"] = "fake/demo"

from pi.server.app import create_app  # noqa: E402
from pi.server.config import ServerSettings  # noqa: E402


def main() -> int:
    out = REPO_ROOT / "web" / "openapi.json"
    try:
        app = create_app(ServerSettings.from_env())
        schema = app.openapi()
        out.parent.mkdir(parents=True, exist_ok=True)
        # sort_keys so the committed file diffs cleanly instead of reshuffling
        out.write_text(
            json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
    paths = schema.get("paths", {})
    schemas = schema.get("components", {}).get("schemas", {})
    print(f"wrote {out.relative_to(REPO_ROOT)}: {len(paths)} paths, {len(schemas)} component schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
