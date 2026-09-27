"""Official async client (SDK) for the pi-py multi-user agent service.

Minimal and dependency-light (httpx only, already a server dependency): wraps
auth, sessions, messages, trajectories, usage and the SSE run stream into a
typed async interface. See README / PROJECT_GUIDE for usage samples.

    async with PiClient("http://localhost:8300") as pi:
        await pi.login("zhu", "...")
        sid = (await pi.create_session("demo"))["id"]
        async for frame in pi.run(sid, "写一个 hello.txt"):
            print(frame["event"])            # start / text_delta / toolcall_* / ...

The client never hides server errors: non-2xx raises PiError with the detail.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

__all__ = ["PiClient", "PiError"]


class PiError(RuntimeError):
    """Server returned a non-2xx status (detail from the API body)."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class PiClient:
    def __init__(
        self,
        base_url: str = "http://localhost:8300",
        token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # connect fast, but let the request body stream for long runs.
        # trust_env=False: ambient proxy vars must not hijack API traffic
        # (ARCHITECTURE §17.4 - a dead SOCKS proxy made every call fail).
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=httpx.Timeout(600.0, connect=10.0),
            trust_env=False,
        )
        self.token = token

    # ---- lifecycle ----------------------------------------------------------
    async def __aenter__(self) -> "PiClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- internals ----------------------------------------------------------
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if extra:
            headers.update(extra)
        return headers

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        resp = await self._client.request(method, path, headers=self._headers(kw.pop("headers", None)), **kw)
        if resp.status_code >= 400:
            detail = resp.status_code
            try:
                detail = resp.json().get("detail", detail)
            except Exception:  # noqa: BLE001 - non-JSON error body
                detail = resp.text[:200] or detail
            raise PiError(resp.status_code, str(detail))
        return resp

    # ---- auth ---------------------------------------------------------------
    async def register(self, username: str, password: str) -> dict[str, Any]:
        """Open signup; returns {"id", "username", "is_admin"}."""
        resp = await self._request(
            "POST", "/v1/auth/register", json={"username": username, "password": password}
        )
        return resp.json()

    async def login(self, username: str, password: str) -> str:
        """Login and store the token on the client; returns it."""
        resp = await self._request(
            "POST", "/v1/auth/login", json={"username": username, "password": password}
        )
        self.token = resp.json()["access_token"]
        return self.token

    async def logout(self) -> None:
        """Revoke the current token (best-effort semantics on the server side)."""
        await self._request("POST", "/v1/auth/logout")

    # ---- sessions -----------------------------------------------------------
    async def create_session(self, title: str = "session", model: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"title": title}
        if model:
            body["model"] = model
        resp = await self._request("POST", "/v1/sessions", json=body)
        return resp.json()

    async def list_sessions(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "/v1/sessions")
        return resp.json()["sessions"]

    async def delete_session(self, session_id: str) -> None:
        await self._request("DELETE", f"/v1/sessions/{session_id}")

    # ---- data ---------------------------------------------------------------
    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        """Full message history: [{idx, role, blocks: [...]}]."""
        resp = await self._request("GET", f"/v1/sessions/{session_id}/messages")
        return resp.json()["messages"]

    async def latest_trajectory(self, session_id: str) -> dict[str, Any]:
        """Latest stored run of a session (canonical trajectory)."""
        resp = await self._request("GET", f"/v1/sessions/{session_id}/trajectory")
        return resp.json()

    async def run_trajectory(self, run_id: str) -> dict[str, Any]:
        """Replay one run by id (ownership-checked)."""
        resp = await self._request("GET", f"/v1/trajectory/{run_id}")
        return resp.json()

    async def usage(self) -> dict[str, Any]:
        """Monthly usage: per-model breakdown + quota/used tokens."""
        resp = await self._request("GET", "/v1/usage")
        return resp.json()

    # ---- run (SSE stream) ---------------------------------------------------
    async def run(
        self, session_id: str, prompt: str, model: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute one turn, yielding parsed SSE frames {"event", "data"}.

        Events: start / text_delta / toolcall_start / toolcall_end /
        compaction / turn_end / error / done.
        """
        body: dict[str, Any] = {"prompt": prompt}
        if model:
            body["model"] = model
        async with self._client.stream(
            "POST",
            f"/v1/sessions/{session_id}/runs",
            json=body,
            headers=self._headers({"Content-Type": "application/json"}),
        ) as resp:
            if resp.status_code >= 400:
                raise PiError(resp.status_code, (await resp.aread()).decode(errors="replace")[:200])
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                while "\n\n" in buf:
                    frame, buf = buf.split("\n\n", 1)
                    event, data = "", ""
                    for line in frame.split("\n"):
                        if line.startswith("event: "):
                            event = line[7:].strip()
                        elif line.startswith("data: "):
                            data += line[6:]
                    if event:
                        yield {"event": event, "data": json.loads(data) if data else {}}

    async def ask(
        self, session_id: str, prompt: str, model: str | None = None
    ) -> dict[str, Any]:
        """Convenience: run to completion, collect text and tool activity."""
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        frames: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        async for frame in self.run(session_id, prompt, model=model):
            frames.append(frame)
            if frame["event"] == "text_delta":
                text_parts.append(frame["data"]["text"])
            elif frame["event"] == "toolcall_end":
                tool_calls.append(frame["data"])
            elif frame["event"] == "turn_end":
                usage = frame["data"]
        return {
            "text": "".join(text_parts),
            "tool_calls": tool_calls,
            "usage": usage,
            "frames": frames,
        }
