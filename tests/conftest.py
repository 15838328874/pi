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
# The repo root has a real .env with live Milvus credentials and a live rerank
# endpoint. Unpinned, the suite would talk to a production vector database, a test
# that calls setup() would create a collection in it, and any retrieval would bill
# rerank calls against a real API key. Empty URI -> NoOpStore, empty model -> no
# embedder, empty rerank URL -> no reranker, so memory is off unless a test opts in
# explicitly.
os.environ["PI_MILVUS_URI"] = ""
os.environ["PI_EMBEDDING_MODEL"] = ""
os.environ["PI_RERANK_URL"] = ""
# The memory models too: .env carries live gateway model ids (extraction flash,
# arbitration plus). Left unpinned, an app-level test that reaches record() or
# the arbiter loop without stubbing its provider would silently bill the real
# gateway. Empty model = record() reuses the run's (fake) provider, and the
# arbiter loop never starts.
os.environ["PI_MEMORY_MODEL"] = ""
os.environ["PI_MEMORY_ARBITER_MODEL"] = ""
# app.py mounts the built frontend at "/" when web/dist exists. Without this pin
# the suite's routing would depend on whether someone happened to run
# `npm run build`, and a catch-all mount changes how every other route resolves.
# TestWebUiMount points PI_WEB_DIST at a real directory itself.
os.environ["PI_WEB_DIST"] = "/nonexistent-pi-web-dist"

from pi.llm.fake import FakeProvider  # noqa: E402  (after the env pinning above)
from pi.models import ToolCallBlock, ToolResultBlock  # noqa: E402


class StrictFakeProvider(FakeProvider):
    """FakeProvider plus the pairing rule OpenAI and Anthropic both enforce.

    A real provider rejects a request whose assistant message contains a tool_call
    with no matching tool_result in the following user message - and rejects it on
    the *next* request, not the one that produced the batch. Tests that run this
    provider over reloaded history therefore fail where the bug actually is.
    """

    async def stream(self, system, messages, tools):
        for i, msg in enumerate(messages):
            ids = {b.id for b in msg.blocks if isinstance(b, ToolCallBlock)}
            if not ids:
                continue
            following = messages[i + 1].blocks if i + 1 < len(messages) else []
            answered = {b.tool_use_id for b in following if isinstance(b, ToolResultBlock)}
            if answered != ids:
                raise ValueError(f"tool_calls without a tool_result: {sorted(ids - answered)}")
        async for ev in super().stream(system, messages, tools):
            yield ev
