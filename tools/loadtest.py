"""Load test for the pi-py server: concurrency, locks, latency percentiles.

Usage:
    python tools/loadtest.py --url http://127.0.0.1:8300 --users 20 --rounds 3
    python tools/loadtest.py --url http://127.0.0.1:8300 --same-session 5

Scenarios:
    default          N virtual users, each with its own session, R rounds each
    --same-session K concurrent requests against ONE session (lock contention)
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


async def setup_user(client: httpx.AsyncClient, url: str, i: int) -> tuple[str, str]:
    username = f"loadtest_{i}_{int(time.time())}"
    r = await client.post(
        f"{url}/v1/auth/register",
        json={"username": username, "password": "loadtest-pass-123"},
    )
    if r.status_code not in (200, 409):
        raise SystemExit(f"registration failed ({r.status_code}): {r.text[:200]}")
    r = await client.post(f"{url}/v1/auth/login", json={"username": username, "password": "loadtest-pass-123"})
    token = r.json()["access_token"]
    sid = (
        await client.post(
            f"{url}/v1/sessions",
            json={"title": "loadtest"},
            headers={"Authorization": f"Bearer {token}"},
        )
    ).json()["id"]
    return token, sid


async def one_run(client: httpx.AsyncClient, url: str, token: str, sid: str, timeout: float = 120.0) -> tuple[float, int, bool]:
    """Returns (duration, status_code, lock_rejected).

    Lock rejection arrives as an SSE `error` event inside a 200 response,
    so the body must be inspected, not just the status code. status_code 0
    means a transport error (timeout / dropped stream) - real models need a
    bigger timeout than the fake-model default.
    """
    start = time.perf_counter()
    try:
        r = await client.post(
            f"{url}/v1/sessions/{sid}/runs",
            # Deliberately trivial: the harness measures the SERVICE, not the
            # model. An open-ended prompt like "load test round" makes a real
            # model treat it as a coding task - it then spins on sandbox-denied
            # absolute-path writes until the run timeout (observed live: 25
            # denials, several 600s timeouts). To stress model pathology
            # instead, run with a real task prompt and expect long tail
            # latency.
            json={"prompt": "Reply with exactly: OK"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        body = r.text
    except httpx.HTTPError:
        return time.perf_counter() - start, 0, False
    rejected = "already running" in body
    return time.perf_counter() - start, r.status_code, rejected


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(len(ordered) * p))
    return ordered[idx]


async def scenario_multi_user(url: str, users: int, rounds: int, timeout: float) -> None:
    latencies: list[float] = []
    status_codes: list[int] = []

    async with httpx.AsyncClient(timeout=timeout + 60.0, trust_env=False) as client:
        async def worker(i: int):
            token, sid = await setup_user(client, url, i)
            for r in range(rounds):
                dur, code, _ = await one_run(client, url, token, sid, timeout=timeout)
                latencies.append(dur)
                status_codes.append(code)

        await asyncio.gather(*(worker(i) for i in range(users)))

    await _report("multi-user", latencies, status_codes)


async def scenario_same_session(url: str, concurrency: int) -> None:
    latencies: list[float] = []
    status_codes: list[int] = []
    lock_rejections = 0

    async with httpx.AsyncClient(timeout=180.0, trust_env=False) as client:
        token, sid = await setup_user(client, url, 9999)
        results = await asyncio.gather(
            *(one_run(client, url, token, sid) for _ in range(concurrency))
        )
        for dur, code, rejected in results:
            latencies.append(dur)
            status_codes.append(code)
            if rejected:
                lock_rejections += 1

    await _report(f"same-session (x{concurrency} concurrent)", latencies, status_codes)
    print(f"    executed: {len(latencies) - lock_rejections}  lock-rejected: {lock_rejections}")
    print(f"    note: concurrent same-session requests serialize on the session lock;")


async def _report(name: str, latencies: list[float], status_codes: list[int]) -> None:
    print(f"\n== {name} ==")
    print(f"    requests: {len(latencies)}")
    codes: dict[int, int] = {}
    for c in status_codes:
        codes[c] = codes.get(c, 0) + 1
    print(f"    status codes: {codes}")
    if latencies:
        print(
            f"    latency  avg={statistics.mean(latencies)*1000:.0f}ms"
            f"  p50={pct(latencies, 0.5)*1000:.0f}ms"
            f"  p95={pct(latencies, 0.95)*1000:.0f}ms"
            f"  p99={pct(latencies, 0.99)*1000:.0f}ms"
            f"  max={max(latencies)*1000:.0f}ms"
        )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8300")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120.0, help="per-run request timeout; real models need 300+")
    parser.add_argument("--same-session", type=int, default=0, help="concurrent requests on one session")
    args = parser.parse_args()

    start = time.perf_counter()
    if args.same_session:
        await scenario_same_session(args.url, args.same_session)
    else:
        await scenario_multi_user(args.url, args.users, args.rounds, args.timeout)
    print(f"\ntotal wall time: {time.perf_counter() - start:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
