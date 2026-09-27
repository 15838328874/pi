import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# pi/__init__.py auto-loads ./.env on import, and existing environment variables
# win. Pin these before any test module imports pi so a production .env sitting in
# the repo root cannot point the suite at real Redis/Docker or write outside tmp_path.
os.environ["PI_REDIS_URL"] = ""
os.environ["PI_SANDBOX"] = ""
os.environ["PI_POLICY"] = ""
os.environ["PI_TRACER"] = "noop"
os.environ["PI_EMBEDDING_URL"] = ""
os.environ["PI_EMBEDDING_API_KEY"] = ""
os.environ["PI_EMBEDDING_MODEL"] = ""
os.environ["PI_MILVUS_URI"] = ""
os.environ["PI_MCP_SERVERS"] = ""
os.environ["PI_SKILLS_DIR"] = ""
