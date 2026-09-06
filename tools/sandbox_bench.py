"""Sandbox capacity benchmark: how many concurrent users the warm pool can serve.

Drives `pi.tools.sandbox.DockerPool` directly - no HTTP, no LLM, no database and
no `PI_MAX_CONCURRENT_RUNS` semaphore - so the numbers describe the sandbox layer
alone. One workspace directory == one user == one warm container.

Each sweep point measures two phases, because they have different bottlenecks:
  cold    all N containers created at once (thundering herd, throttled by
          PI_SANDBOX_CREATE_CONCURRENCY) - what happens when N users arrive together
  steady  M bash calls per user against already-warm containers - the real
          per-user serving cost

Usage:
    python tools/sandbox_bench.py
    python tools/sandbox_bench.py --sweep 1,4,16,32 --calls 20 --pool-max 16,64
    python tools/sandbox_bench.py --command 'python -c "print(sum(range(10000)))"'
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from pi.tools.sandbox import DockerPool

IMAGE = os.environ.get("PI_SANDBOX_IMAGE", "python:3.12-slim")
DEFAULT_COMMAND = "ls -la /ws"


class _SweepAborted(Exception):
    """Control flow: stop sweeping, still print the summary of what completed."""


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def _cpu_times() -> tuple[int, int]:
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:9]]
    return sum(vals), vals[3] + vals[4]  # total, idle+iowait


def _mem_available_mib() -> float:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    return 0.0


@dataclass
class HostSample:
    cpu_busy: float  # fraction over the sampling interval
    mem_avail_mib: float
    pool_entries: int


class HostSampler:
    """Samples host CPU/memory and live pool size while a phase runs."""

    def __init__(self, pool: DockerPool, interval: float = 0.05):
        self.pool = pool
        self.interval = interval
        self.samples: list[HostSample] = []
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self.samples.clear()
        self._task = asyncio.ensure_future(self._loop())

    async def _loop(self) -> None:
        prev_total, prev_idle = _cpu_times()
        while True:
            await asyncio.sleep(self.interval)
            total, idle = _cpu_times()
            d_total, d_idle = total - prev_total, idle - prev_idle
            prev_total, prev_idle = total, idle
            busy = 1.0 - (d_idle / d_total) if d_total > 0 else 0.0
            # _entries is private; a bench tool reading it beats shelling out to
            # `docker ps` on every sample, which would itself perturb the result.
            self.samples.append(HostSample(busy, _mem_available_mib(), len(self.pool._entries)))

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


class _WarnCounter(logging.Handler):
    """Counts pool warnings (LRU eviction pressure / overshoot)."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.overshoot = 0
        self.other: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "overshoot" in msg:
            self.overshoot += 1
        else:
            self.other.append(msg[:120])


@dataclass
class PointResult:
    n: int
    pool_max: int
    calls: int
    cold_total: float
    steady_wall: float
    latencies: list[float] = field(default_factory=list)
    exit_nonzero: int = 0
    error_outputs: int = 0
    samples: list[HostSample] = field(default_factory=list)
    overshoot: int = 0
    warnings: list[str] = field(default_factory=list)
    containers_after: int = 0
    mem_baseline_mib: float = 0.0

    @property
    def total_calls(self) -> int:
        return self.n * self.calls

    @property
    def throughput(self) -> float:
        return self.total_calls / self.steady_wall if self.steady_wall > 0 else 0.0

    def _cpu(self, peak: bool) -> float:
        if not self.samples:
            return 0.0
        vals = [s.cpu_busy for s in self.samples]
        return (max(vals) if peak else statistics.mean(vals)) * 100

    def _mem_used_mib(self) -> float:
        if not self.samples:
            return 0.0
        return self.mem_baseline_mib - min(s.mem_avail_mib for s in self.samples)

    def report(self) -> str:
        lat = [x * 1000 for x in self.latencies]
        lines = [
            f"  N={self.n:<4} pool_max={self.pool_max:<4} {self.total_calls} calls "
            f"({self.calls}/user)",
            f"    cold   : {self.cold_total:6.2f}s for {self.n} containers "
            f"({self.cold_total / self.n * 1000:6.0f}ms each)",
            f"    steady : {self.steady_wall:6.2f}s -> {self.throughput:6.1f} calls/s",
            f"    latency: p50={_pct(lat, 0.5):6.0f}ms  p90={_pct(lat, 0.9):6.0f}ms  "
            f"p95={_pct(lat, 0.95):6.0f}ms  p99={_pct(lat, 0.99):6.0f}ms  "
            f"max={max(lat) if lat else 0:6.0f}ms",
            f"    failures: exit!=0 {self.exit_nonzero}  'Error:' output {self.error_outputs}"
            f"   containers left: {self.containers_after}",
            f"    host   : cpu avg={self._cpu(False):5.1f}% peak={self._cpu(True):5.1f}%   "
            f"mem used={self._mem_used_mib():7.0f}MiB   pool entries max="
            f"{max((s.pool_entries for s in self.samples), default=0)}",
        ]
        if self.overshoot:
            lines.append(f"    !! pool exceeded pool_max {self.overshoot}x (overshoot allowed)")
        for w in self.warnings[:3]:
            lines.append(f"    !! {w}")
        return "\n".join(lines)


async def run_point(
    n: int,
    calls: int,
    command: str,
    pool_max: int,
    create_concurrency: int,
    timeout: int,
    ws_root: Path,
) -> PointResult:
    pool = DockerPool(
        image=IMAGE,
        allow_network=False,
        pool_max=pool_max,
        idle_ttl=3600.0,  # keep the sweeper out of the measurement
        create_concurrency=create_concurrency,
        warm_lifetime=1800,
    )
    counter = _WarnCounter()
    logging.getLogger("pi.sandbox").addHandler(counter)
    sampler = HostSampler(pool)
    dirs = [ws_root / f"u{i:04d}" for i in range(n)]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    result = PointResult(n=n, pool_max=pool_max, calls=calls, cold_total=0.0, steady_wall=0.0)
    result.mem_baseline_mib = _mem_available_mib()

    try:
        sampler.start()
        t0 = time.perf_counter()
        await asyncio.gather(*(pool.acquire(d) for d in dirs))
        result.cold_total = time.perf_counter() - t0

        async def user(d: Path) -> None:
            for _ in range(calls):
                start = time.perf_counter()
                r = await pool.run(command, d, timeout)
                result.latencies.append(time.perf_counter() - start)
                if r.exit_code != 0:
                    result.exit_nonzero += 1
                if r.output.startswith("Error:"):
                    result.error_outputs += 1

        t1 = time.perf_counter()
        await asyncio.gather(*(user(d) for d in dirs))
        result.steady_wall = time.perf_counter() - t1
    finally:
        sampler.stop()
        result.samples = list(sampler.samples)
        result.overshoot = counter.overshoot
        result.warnings = counter.other
        logging.getLogger("pi.sandbox").removeHandler(counter)
        await pool.shutdown()
        result.containers_after = len(pool._entries)
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
    return result


async def count_warm_containers() -> int:
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-q", "--filter", "name=pi-py-warm-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return len(out.decode().split())


def print_summary(results: list[PointResult], command: str) -> None:
    print(f"\n{'=' * 100}\nSUMMARY  image={IMAGE}  command={command!r}\n{'=' * 100}")
    print(
        f"{'pool':>5} {'N':>4} {'cold_s':>7} {'cold_ms/ctr':>12} {'calls/s':>8} "
        f"{'p50ms':>7} {'p95ms':>7} {'p99ms':>7} {'max_ms':>7} {'fail':>5} "
        f"{'cpu_avg':>8} {'cpu_pk':>7} {'mem_MiB':>8}"
    )
    for r in results:
        lat = [x * 1000 for x in r.latencies]
        print(
            f"{r.pool_max:>5} {r.n:>4} {r.cold_total:>7.2f} {r.cold_total / r.n * 1000:>12.0f} "
            f"{r.throughput:>8.1f} {_pct(lat, 0.5):>7.0f} {_pct(lat, 0.95):>7.0f} "
            f"{_pct(lat, 0.99):>7.0f} {max(lat) if lat else 0:>7.0f} "
            f"{r.exit_nonzero + r.error_outputs:>5} {r._cpu(False):>7.1f}% "
            f"{r._cpu(True):>6.1f}% {r._mem_used_mib():>8.0f}"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sweep", default="1,2,4,8,16,24,32,48,64",
                        help="comma-separated concurrent user counts")
    parser.add_argument("--pool-max", default="16",
                        help="comma-separated PI_SANDBOX_POOL_MAX values to compare")
    parser.add_argument("--calls", type=int, default=10, help="bash calls per user per point")
    parser.add_argument("--command", default=DEFAULT_COMMAND, help="command run inside /ws")
    parser.add_argument("--create-concurrency", type=int,
                        default=int(os.environ.get("PI_SANDBOX_CREATE_CONCURRENCY", 4)))
    parser.add_argument("--timeout", type=int, default=30, help="per-command timeout (s)")
    parser.add_argument("--ws-root", default="/root/.pi-py/sandbox-bench")
    parser.add_argument("--min-free-mib", type=float, default=2048.0,
                        help="abort the sweep if MemAvailable drops below this")
    parser.add_argument("--cleanup-orphans", action="store_true",
                        help="remove any leftover pi-py-warm-* containers at the end")
    args = parser.parse_args()

    sweep = [int(x) for x in args.sweep.split(",") if x.strip()]
    pool_maxes = [int(x) for x in args.pool_max.split(",") if x.strip()]
    ws_root = Path(args.ws_root)
    ws_root.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    print(f"sandbox bench: image={IMAGE} sweep={sweep} pool_max={pool_maxes} "
          f"calls/user={args.calls} create_concurrency={args.create_concurrency}")
    print(f"command={args.command!r}  workspaces={ws_root}  cpus={os.cpu_count()}")

    results: list[PointResult] = []
    started = time.perf_counter()
    try:
        for pool_max in pool_maxes:
            for n in sweep:
                free = _mem_available_mib()
                if free < args.min_free_mib:
                    print(f"\nABORTING sweep: MemAvailable {free:.0f}MiB < "
                          f"{args.min_free_mib}MiB floor; refusing to risk OOM-killing dockerd "
                          f"and orphaning warm containers. Remaining points skipped.")
                    raise _SweepAborted
                r = await run_point(n, args.calls, args.command, pool_max,
                                    args.create_concurrency, args.timeout, ws_root)
                results.append(r)
                print(r.report(), flush=True)
    except _SweepAborted:
        pass
    finally:
        print_summary(results, args.command)
        print(f"\ntotal wall time: {time.perf_counter() - started:.1f}s")
        left = await count_warm_containers()
        print(f"warm containers still alive after shutdown: {left}")
        if left and args.cleanup_orphans:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f",
                *(await _warm_ids()),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            print(f"removed; now {await count_warm_containers()} left")
        elif left:
            print("  (re-run with --cleanup-orphans to remove them)")
        shutil.rmtree(ws_root, ignore_errors=True)


async def _warm_ids() -> list[str]:
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-q", "--filter", "name=pi-py-warm-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode().split()


if __name__ == "__main__":
    asyncio.run(main())
