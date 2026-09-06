"""Tool execution sandbox: local shell (default) or Docker container isolation.

PI_SANDBOX=docker routes bash commands into Docker containers. Two modes:

- pooled (default; PI_SANDBOX_POOL=0 disables): one warm container per
  workspace. The container is preheated at turn start (concurrent with the
  LLM's first response), each bash call is a fast `docker exec` into it, and
  the container is recycled when idle (PI_SANDBOX_IDLE_TTL), LRU-evicted at
  capacity (PI_SANDBOX_POOL_MAX), or rebuilt after a timeout. Commands in the
  same workspace share process state (installed packages, env vars) - state
  resets only when the container is recycled. The workspace is still just a
  bind mount, so files always persist.
- cold (legacy): a fresh `docker run --rm` per invocation; pristine process
  state every call, full container lifecycle cost every call.

Both modes: the session workspace is bind-mounted read-write at /ws with
workdir /ws, no network by default (PI_SANDBOX_NET=host to allow), and
fail-closed behaviour when docker is configured but unreachable.

Warm containers self-terminate after PI_SANDBOX_WARM_LIFETIME (default 2h) as
an orphan safety net - a crashed app cannot leave them running forever.

File tools (read/write/edit/...) keep operating on the host workspace dir -
the same directory the container mounts - and stay guarded by the path
sandbox policy.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger("pi.sandbox")

_WARM_LIFETIME_DEFAULT = 7200  # seconds; hard cap on any single warm container


class SandboxUnavailableError(RuntimeError):
    """Docker daemon/transport failure - the sandbox cannot run the command."""


class ExecTimeoutError(Exception):
    """The sandboxed command exceeded its timeout."""


class ContainerGoneError(Exception):
    """The warm container vanished (daemon restart / lifetime cap / eviction)."""


_SIZE_SUFFIXES = (
    ("kb", 1024), ("mb", 1024**2), ("gb", 1024**3),
    ("k", 1024), ("m", 1024**2), ("g", 1024**3), ("b", 1),
)


def _parse_size(text: str) -> int:
    """'1g' -> 1073741824. The docker CLI takes suffixes; the Engine API takes bytes."""
    s = text.strip().lower()
    if not s:
        return 0
    unit = 1
    for suffix, mult in _SIZE_SUFFIXES:
        if s.endswith(suffix):
            s, unit = s[: -len(suffix)], mult
            break
    try:
        return int(float(s.strip()) * unit)
    except ValueError:
        raise ValueError(f"invalid size {text!r}; expected e.g. 512m or 1g") from None


@dataclass(frozen=True)
class SandboxLimits:
    """Resource ceilings for sandbox containers; empty/zero means "omit the flag".

    Docker's own defaults are no limit at all (Memory=0, NanoCpus=0, no
    PidsLimit, cgroup memory.max=max), and registration is open, so without
    these any account can exhaust the host with one command. Network isolation
    and the per-user bind mount do not cover resource exhaustion.
    """

    memory: str = "1g"
    pids: int = 256
    cpus: str = "1.0"
    user: str = ""

    def __post_init__(self) -> None:
        _parse_size(self.memory)
        if self.cpus:
            try:
                float(self.cpus)
            except ValueError:
                raise ValueError(f"invalid PI_SANDBOX_CPUS {self.cpus!r}; expected e.g. 0.5 or 2") from None

    @classmethod
    def from_env(cls) -> "SandboxLimits":
        return cls(
            memory=os.environ.get("PI_SANDBOX_MEMORY", "1g"),
            pids=int(os.environ.get("PI_SANDBOX_PIDS", "256")),
            cpus=os.environ.get("PI_SANDBOX_CPUS", "1.0"),
            user=os.environ.get("PI_SANDBOX_USER", ""),
        )

    @property
    def effective_user(self) -> str:
        """The app's own uid:gid unless PI_SANDBOX_USER overrides it.

        Files written through the bind mount land on the host owned by whoever
        ran them inside. Tracking the app's uid keeps those files readable and
        deletable by the app; a container running as root would otherwise leave
        root-owned files behind that a non-root app (compose runs as uid 10001)
        cannot clean up.
        """
        if self.user:
            return self.user
        return f"{os.getuid()}:{os.getgid()}"

    @property
    def memory_bytes(self) -> int:
        return _parse_size(self.memory)

    @property
    def nano_cpus(self) -> int:
        return int(float(self.cpus) * 1_000_000_000) if self.cpus else 0

    def cli_flags(self) -> list[str]:
        flags: list[str] = []
        if self.memory:
            # --memory-swap equal to --memory disables swap; docker's default
            # would otherwise allow 2x the memory limit.
            flags += ["--memory", self.memory, "--memory-swap", self.memory]
        if self.pids > 0:
            flags += ["--pids-limit", str(self.pids)]
        if self.cpus:
            flags += ["--cpus", self.cpus]
        user = self.effective_user
        if user:
            flags += ["--user", user]
        return flags

    def host_config(self) -> dict:
        cfg: dict = {}
        if self.memory:
            cfg["Memory"] = self.memory_bytes
            cfg["MemorySwap"] = self.memory_bytes
        if self.pids > 0:
            cfg["PidsLimit"] = self.pids
        if self.nano_cpus:
            cfg["NanoCpus"] = self.nano_cpus
        return cfg


@dataclass
class CommandResult:
    output: str
    exit_code: int
    timed_out: bool = False


class CommandRunner(Protocol):
    async def run(self, command: str, cwd: Path, timeout: int) -> CommandResult: ...

    def prewarm(self, cwd: Path) -> None: ...


class LocalRunner:
    """Direct shell execution (single-user / trusted mode)."""

    async def run(self, command: str, cwd: Path, timeout: int) -> CommandResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:  # noqa: BLE001
            return CommandResult(output=f"Error: failed to spawn command: {exc}", exit_code=-1)

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return CommandResult(
                output=f"Error: command timed out after {timeout}s: {command}",
                exit_code=-1,
                timed_out=True,
            )
        text = _decode(stdout)
        return CommandResult(output=text.strip() or "(no output)", exit_code=proc.returncode or 0)

    def prewarm(self, cwd: Path) -> None:
        """No-op: local execution has nothing to warm."""


class DockerRunner:
    """Cold-path docker sandbox: one `docker run --rm` per invocation.

    Two transports:
    - docker CLI (default, when available)
    - Docker Engine REST API (PI_DOCKER_HOST=tcp://host:2375) - no CLI needed,
      works against a remote daemon.
    """

    def __init__(
        self,
        image: str = "python:3.12-slim",
        allow_network: bool = False,
        docker_host: str | None = None,
        limits: SandboxLimits | None = None,
    ):
        self.image = image
        self.allow_network = allow_network
        self.limits = limits if limits is not None else SandboxLimits.from_env()
        self.docker_host = docker_host or os.environ.get("PI_DOCKER_HOST", "")
        if not self.docker_host and shutil.which("docker") is None:
            raise RuntimeError(
                "PI_SANDBOX=docker but neither the docker CLI nor PI_DOCKER_HOST is available"
            )

    async def run(self, command: str, cwd: Path, timeout: int) -> CommandResult:
        if self.docker_host:
            return await self._run_via_api(command, cwd, timeout)
        return await self._run_via_cli(command, cwd, timeout)

    def prewarm(self, cwd: Path) -> None:
        """No-op: cold path starts a fresh container per call anyway."""

    # --- CLI transport ----------------------------------------------------

    async def _run_via_cli(self, command: str, cwd: Path, timeout: int) -> CommandResult:
        net = [] if self.allow_network else ["--network", "none"]
        argv = [
            "docker", "run", "--rm",
            "-v", f"{cwd}:/ws",
            "-w", "/ws",
            *net,
            *self.limits.cli_flags(),
            self.image,
            "sh", "-lc", command,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:  # noqa: BLE001
            return CommandResult(output=f"Error: sandbox spawn failed: {exc}", exit_code=-1)

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 15)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return CommandResult(
                output=f"Error: sandboxed command timed out after {timeout}s: {command}",
                exit_code=-1,
                timed_out=True,
            )
        text = _decode(stdout)
        body = text.strip() or "(no output)"
        return CommandResult(output=body, exit_code=proc.returncode or 0)

    # --- Engine REST API transport ----------------------------------------

    async def _run_via_api(self, command: str, cwd: Path, timeout: int) -> CommandResult:
        import httpx

        base = self.docker_host.replace("tcp://", "http://").rstrip("/")
        async with httpx.AsyncClient(base_url=base, timeout=timeout + 60.0) as client:
            container_id: str | None = None
            try:
                await self._ensure_image(client, self.image)
                create = await client.post(
                    "/containers/create",
                    json={
                        "Image": self.image,
                        "Cmd": ["sh", "-lc", command],
                        "WorkingDir": "/ws",
                        # Config.User, not HostConfig - docker exec inherits it
                        "User": self.limits.effective_user,
                        "NetworkMode": "bridge" if self.allow_network else "none",
                        "HostConfig": {
                            "Binds": [f"{cwd}:/ws"],
                            "AutoRemove": False,
                            # NetworkMode must ALSO be set inside HostConfig -
                            # the top-level field alone is not enforced by the API
                            "NetworkMode": "bridge" if self.allow_network else "none",
                            **self.limits.host_config(),
                        },
                        "AttachStdout": False,
                        "AttachStderr": False,
                        "Tty": False,
                    },
                )
                if create.status_code != 201:
                    return CommandResult(
                        output=f"Error: sandbox create failed ({create.status_code}): {create.text[:200]}",
                        exit_code=-1,
                    )
                container_id = create.json()["Id"]

                started = await client.post(f"/containers/{container_id}/start")
                if started.status_code not in (204, 304):
                    return CommandResult(
                        output=f"Error: sandbox start failed ({started.status_code}): {started.text[:200]}",
                        exit_code=-1,
                    )

                try:
                    wait = await asyncio.wait_for(
                        client.post(f"/containers/{container_id}/wait"), timeout=timeout
                    )
                    exit_code = int(wait.json().get("StatusCode", -1))
                except (asyncio.TimeoutError, TimeoutError):
                    try:
                        await client.post(f"/containers/{container_id}/kill")
                    finally:
                        return CommandResult(
                            output=f"Error: sandboxed command timed out after {timeout}s: {command}",
                            exit_code=-1,
                            timed_out=True,
                        )

                logs = await client.get(
                    f"/containers/{container_id}/logs", params={"stdout": 1, "stderr": 1}
                )
                text = _demux_docker_logs(logs.content)
                body = text.strip() or "(no output)"
                return CommandResult(output=body, exit_code=exit_code)
            except httpx.HTTPError as exc:
                return CommandResult(output=f"Error: sandbox API failed: {exc}", exit_code=-1)
            finally:
                if container_id:
                    try:
                        await client.delete(f"/containers/{container_id}", params={"force": 1})
                    except httpx.HTTPError:
                        pass

    async def _ensure_image(self, client, image: str) -> None:
        probe = await client.get(f"/images/{image}/json")
        if probe.status_code == 200:
            return
        pull = await client.post("/images/create", params={"fromImage": image}, timeout=600.0)
        if pull.status_code not in (200, 201):
            raise RuntimeError(f"failed to pull sandbox image {image}: {pull.text[:200]}")


# ---------------------------------------------------------------------------
# Warm pool
# ---------------------------------------------------------------------------


class CliTransport:
    """Warm-container lifecycle over the docker CLI."""

    def __init__(self, *, image: str, allow_network: bool, warm_lifetime: int,
                 limits: SandboxLimits | None = None):
        if shutil.which("docker") is None:
            raise RuntimeError(
                "PI_SANDBOX=docker but neither the docker CLI nor PI_DOCKER_HOST is available"
            )
        self.image = image
        self.net = [] if allow_network else ["--network", "none"]
        self.warm_lifetime = warm_lifetime
        self.limits = limits if limits is not None else SandboxLimits.from_env()

    async def create_warm(self, bind: str, workdir: str) -> str:
        name = f"pi-py-warm-{uuid.uuid4().hex[:12]}"
        argv = [
            "docker", "run", "-d", "--rm", "--name", name,
            "-v", f"{bind}:/ws",
            "-w", workdir,
            *self.net,
            *self.limits.cli_flags(),
            self.image,
            "timeout", str(self.warm_lifetime), "sleep", "infinity",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:  # noqa: BLE001
            raise SandboxUnavailableError(f"failed to spawn docker: {exc}") from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            raise SandboxUnavailableError("docker run timed out (first image pull too slow?)")
        if proc.returncode != 0:
            raise SandboxUnavailableError(
                f"docker run failed ({proc.returncode}): {_decode(err)[:200]}"
            )
        cid = _decode(out).strip().splitlines()[-1] if out.strip() else ""
        if not cid:
            raise SandboxUnavailableError("docker run returned no container id")
        return cid

    async def exec(self, container_id: str, command: str, workdir: str, timeout: int) -> tuple[str, int]:
        argv = ["docker", "exec", "-w", workdir, container_id, "sh", "-lc", command]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:  # noqa: BLE001
            raise SandboxUnavailableError(f"failed to spawn docker exec: {exc}") from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout + 10)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise ExecTimeoutError()
        rc = proc.returncode or 0
        err_text = _decode(err)
        if rc == 125 and err_text:
            if "No such container" in err_text or "is not running" in err_text:
                raise ContainerGoneError(err_text.strip()[:200])
            raise SandboxUnavailableError(f"docker exec failed: {err_text[:200]}")
        return _decode(out).strip(), rc

    async def remove(self, container_id: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", container_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)
        except Exception:  # noqa: BLE001 - removal is best-effort
            log.debug("docker rm failed for %s", container_id, exc_info=True)


class ApiTransport:
    """Warm-container lifecycle over the Docker Engine REST API."""

    def __init__(self, docker_host: str, *, image: str, allow_network: bool,
                 warm_lifetime: int, limits: SandboxLimits | None = None):
        self.base = docker_host.replace("tcp://", "http://").rstrip("/")
        self.image = image
        self.net_mode = "bridge" if allow_network else "none"
        self.warm_lifetime = warm_lifetime
        self.limits = limits if limits is not None else SandboxLimits.from_env()

    async def create_warm(self, bind: str, workdir: str) -> str:
        import httpx

        async with httpx.AsyncClient(base_url=self.base, timeout=600.0) as client:
            await _ensure_image(client, self.image)
            create = await client.post(
                "/containers/create",
                json={
                    "Image": self.image,
                    "Cmd": ["timeout", str(self.warm_lifetime), "sleep", "infinity"],
                    "WorkingDir": workdir,
                    # Config.User, not HostConfig - docker exec inherits it
                    "User": self.limits.effective_user,
                    "NetworkMode": self.net_mode,
                    "HostConfig": {
                        "Binds": [f"{bind}:/ws"],
                        "AutoRemove": True,
                        "NetworkMode": self.net_mode,
                        **self.limits.host_config(),
                    },
                    "AttachStdout": False,
                    "AttachStderr": False,
                    "Tty": False,
                },
            )
            if create.status_code != 201:
                raise SandboxUnavailableError(
                    f"container create failed ({create.status_code}): {create.text[:200]}"
                )
            cid = create.json()["Id"]
            started = await client.post(f"/containers/{cid}/start")
            if started.status_code not in (204, 304):
                raise SandboxUnavailableError(
                    f"container start failed ({started.status_code}): {started.text[:200]}"
                )
            return cid

    async def exec(self, container_id: str, command: str, workdir: str, timeout: int) -> tuple[str, int]:
        import httpx

        async with httpx.AsyncClient(base_url=self.base, timeout=timeout + 60.0) as client:
            created = await client.post(
                f"/containers/{container_id}/exec",
                json={
                    "Cmd": ["sh", "-lc", command],
                    "WorkingDir": workdir,
                    "AttachStdout": True,
                    "AttachStderr": True,
                },
            )
            if created.status_code in (404, 409):
                raise ContainerGoneError(f"exec create {created.status_code}: {created.text[:120]}")
            if created.status_code != 201:
                raise SandboxUnavailableError(
                    f"exec create failed ({created.status_code}): {created.text[:200]}"
                )
            exec_id = created.json()["Id"]
            buf = bytearray()

            async def _drain() -> None:
                async with client.stream(
                    "POST", f"/exec/{exec_id}/start", json={"Detach": False, "Tty": False}
                ) as resp:
                    if resp.status_code in (404, 409):
                        raise ContainerGoneError(f"exec start {resp.status_code}")
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)

            try:
                await asyncio.wait_for(_drain(), timeout=timeout + 10)
            except asyncio.TimeoutError:
                raise ExecTimeoutError()
            except httpx.HTTPError as exc:
                raise SandboxUnavailableError(f"exec stream failed: {exc}") from exc

            code = -1
            status = await client.get(f"/exec/{exec_id}/json")
            if status.status_code == 200:
                code = int(status.json().get("ExitCode", -1))
            return _demux_docker_logs(bytes(buf)).strip(), code

    async def remove(self, container_id: str) -> None:
        import httpx

        try:
            async with httpx.AsyncClient(base_url=self.base, timeout=30.0) as client:
                await client.delete(f"/containers/{container_id}", params={"force": 1})
        except httpx.HTTPError:
            log.debug("api remove failed for %s", container_id, exc_info=True)


async def _ensure_image(client, image: str) -> None:
    probe = await client.get(f"/images/{image}/json")
    if probe.status_code == 200:
        return
    pull = await client.post("/images/create", params={"fromImage": image}, timeout=600.0)
    if pull.status_code not in (200, 201):
        raise SandboxUnavailableError(f"failed to pull sandbox image {image}: {pull.text[:200]}")


@dataclass
class _PoolEntry:
    key: str
    cwd: Path
    container_id: str | None = None
    create_task: asyncio.Task | None = None
    busy: int = 0
    created_at: float = 0.0
    last_used: float = field(default_factory=time.monotonic)


class DockerPool:
    """Warm container pool: one container per workspace, exec per call.

    Lifecycle per entry: prewarm/acquire creates a detached `sleep infinity`
    container (bounded by `timeout` inside it), commands run via `docker exec`,
    and the container is destroyed on idle, LRU eviction, command timeout, or
    shutdown. A container that vanished under us (daemon restart, lifetime
    cap) is transparently rebuilt once.
    """

    def __init__(
        self,
        *,
        image: str = "python:3.12-slim",
        allow_network: bool = False,
        docker_host: str | None = None,
        transport=None,
        pool_max: int | None = None,
        idle_ttl: float | None = None,
        create_concurrency: int | None = None,
        sweep_interval: float | None = None,
        warm_lifetime: int | None = None,
        limits: SandboxLimits | None = None,
    ):
        self.image = image
        self.allow_network = allow_network
        self.limits = limits if limits is not None else SandboxLimits.from_env()
        self.docker_host = docker_host if docker_host is not None else os.environ.get("PI_DOCKER_HOST", "")
        self.pool_max = pool_max if pool_max is not None else int(os.environ.get("PI_SANDBOX_POOL_MAX", 16))
        self.idle_ttl = idle_ttl if idle_ttl is not None else float(os.environ.get("PI_SANDBOX_IDLE_TTL", 600))
        self.warm_lifetime = (
            warm_lifetime if warm_lifetime is not None
            else int(os.environ.get("PI_SANDBOX_WARM_LIFETIME", _WARM_LIFETIME_DEFAULT))
        )
        cc = (
            create_concurrency if create_concurrency is not None
            else int(os.environ.get("PI_SANDBOX_CREATE_CONCURRENCY", 4))
        )
        if sweep_interval is None:
            sweep_interval = min(60.0, max(1.0, self.idle_ttl / 2))
        self.sweep_interval = sweep_interval
        if transport is None:
            common = dict(
                image=image,
                allow_network=allow_network,
                warm_lifetime=self.warm_lifetime,
                limits=self.limits,
            )
            if self.docker_host:
                transport = ApiTransport(self.docker_host, **common)
            else:
                transport = CliTransport(**common)
        self._transport = transport
        self._entries: dict[str, _PoolEntry] = {}
        self._create_sem = asyncio.Semaphore(cc)
        self._sweeper: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()  # strong refs: prewarm / evict-destroy
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    # --- CommandRunner interface ------------------------------------------

    async def run(self, command: str, cwd: Path, timeout: int) -> CommandResult:
        if self._closed:
            return CommandResult(output="Error: sandbox pool is shut down", exit_code=-1)
        try:
            entry = await self.acquire(cwd)
        except SandboxUnavailableError as exc:
            return CommandResult(output=f"Error: sandbox spawn failed: {exc}", exit_code=-1)

        for attempt in (1, 2):
            try:
                out, code = await self._exec_once(entry, command, timeout)
                return CommandResult(output=out.strip() or "(no output)", exit_code=code)
            except ContainerGoneError as exc:
                await self._drop(entry)
                if attempt == 1:
                    try:
                        entry = await self.acquire(cwd)
                    except SandboxUnavailableError as e2:
                        return CommandResult(output=f"Error: sandbox spawn failed: {e2}", exit_code=-1)
                    continue
                return CommandResult(
                    output=f"Error: sandbox container vanished twice: {exc}", exit_code=-1
                )
            except ExecTimeoutError:
                # container state is dirty (killed mid-command) - recycle it
                await self._drop(entry)
                return CommandResult(
                    output=f"Error: sandboxed command timed out after {timeout}s: {command}",
                    exit_code=-1,
                    timed_out=True,
                )
            except SandboxUnavailableError as exc:
                return CommandResult(output=f"Error: sandbox exec failed: {exc}", exit_code=-1)
        return CommandResult(output="Error: sandbox exec retry loop exhausted", exit_code=-1)

    def prewarm(self, cwd: Path) -> None:
        """Best-effort: start creating the warm container for this workspace now.

        Called at turn start so the container is hot by the time the model
        issues its first bash call. Never raises.
        """
        if self._closed:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._ensure_entry(cwd))
        except RuntimeError:
            return
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.debug("sandbox prewarm failed", exc_info=t.exception())

        task.add_done_callback(_done)

    # --- pool internals ----------------------------------------------------

    def _key(self, cwd: Path) -> str:
        return os.path.normcase(str(Path(cwd).resolve()))

    async def _ensure_entry(self, cwd: Path) -> _PoolEntry:
        key = self._key(cwd)
        entry = self._entries.get(key)
        if entry is None:
            self._evict_if_needed()
            entry = _PoolEntry(key=key, cwd=Path(cwd))
            entry.create_task = asyncio.ensure_future(self._start_entry(entry))
            self._entries[key] = entry
        return entry

    async def _start_entry(self, entry: _PoolEntry) -> None:
        async with self._create_sem:
            if self._closed or entry.container_id:
                return
            bind = str(entry.cwd)
            entry.container_id = await self._transport.create_warm(bind, "/ws")
            entry.created_at = entry.last_used = time.monotonic()

    async def acquire(self, cwd: Path) -> _PoolEntry:
        if self._closed:
            raise SandboxUnavailableError("sandbox pool is shut down")
        self._loop = asyncio.get_running_loop()
        self._ensure_sweeper()
        entry = await self._ensure_entry(cwd)
        if entry.create_task is not None:
            try:
                await entry.create_task
            except Exception as exc:  # noqa: BLE001
                self._entries.pop(entry.key, None)
                raise SandboxUnavailableError(f"warm container create failed: {exc}") from exc
        if not entry.container_id:
            self._entries.pop(entry.key, None)
            raise SandboxUnavailableError("warm container create failed (no id)")
        return entry

    async def _exec_once(self, entry: _PoolEntry, command: str, timeout: int) -> tuple[str, int]:
        entry.busy += 1
        try:
            return await self._transport.exec(entry.container_id or "", command, "/ws", timeout)
        finally:
            entry.busy -= 1
            entry.last_used = time.monotonic()

    def _evict_if_needed(self) -> None:
        """Soft cap: LRU-evict the idlest free entry when at capacity."""
        if len(self._entries) < self.pool_max:
            return
        candidates = [
            e for e in self._entries.values()
            if e.busy == 0 and (e.create_task is None or e.create_task.done())
        ]
        if not candidates:
            log.warning(
                "sandbox pool at max (%d) with all entries busy; allowing temporary overshoot",
                self.pool_max,
            )
            return
        victim = min(candidates, key=lambda e: e.last_used)
        self._entries.pop(victim.key, None)
        self._track_bg(asyncio.ensure_future(self._destroy_container(victim)))

    def _track_bg(self, task: asyncio.Task) -> None:
        """Keep a strong reference to a fire-and-forget task (anti-GC)."""
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _drop(self, entry: _PoolEntry) -> None:
        self._entries.pop(entry.key, None)
        await self._destroy_container(entry)

    async def _destroy_container(self, entry: _PoolEntry) -> None:
        if entry.create_task is not None and not entry.create_task.done():
            entry.create_task.cancel()
            try:
                await entry.create_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        cid = entry.container_id
        entry.container_id = None
        if cid:
            try:
                await self._transport.remove(cid)
            except Exception:  # noqa: BLE001 - removal is best-effort
                log.debug("container removal failed for %s", cid, exc_info=True)

    def _ensure_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.ensure_future(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self.sweep_interval)
            try:
                await self._sweep_once()
            except Exception:  # noqa: BLE001 - the sweeper must survive
                log.exception("sandbox pool sweep failed")

    async def _sweep_once(self) -> None:
        now = time.monotonic()
        # idle entries -> destroy
        stale = [
            e for e in list(self._entries.values())
            if e.busy == 0
            and e.container_id is not None
            and (e.create_task is None or e.create_task.done())
            and now - e.last_used > self.idle_ttl
        ]
        for e in stale:
            self._entries.pop(e.key, None)
            await self._destroy_container(e)
        # entries whose (prewarmed) create failed and nobody awaited -> forget
        broken = [
            e for e in list(self._entries.values())
            if e.create_task is not None and e.create_task.done()
            and not e.create_task.cancelled() and e.create_task.exception() is not None
            and e.busy == 0
        ]
        for e in broken:
            self._entries.pop(e.key, None)

    def _stale_loop(self) -> bool:
        return self._loop is not None and not self._loop.is_running()

    async def shutdown(self) -> None:
        """Destroy every warm container and stop background tasks."""
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # background tasks are short-lived; await them (bounded) so in-flight
        # container removals are not interrupted into orphans
        if self._bg_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(self._bg_tasks), return_exceptions=True), timeout=30.0
                )
            except asyncio.TimeoutError:
                for t in list(self._bg_tasks):
                    t.cancel()
                await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)
        entries = list(self._entries.values())
        self._entries.clear()
        for e in entries:
            await self._destroy_container(e)


_pool: DockerPool | None = None


def _get_pool(image: str, allow_network: bool) -> DockerPool:
    """Process-wide pool; replaced when config changes or its loop died."""
    global _pool
    host = os.environ.get("PI_DOCKER_HOST", "")
    limits = SandboxLimits.from_env()
    if (
        _pool is not None
        and not _pool._closed
        and not _pool._stale_loop()
        and _pool.image == image
        and _pool.allow_network == allow_network
        and _pool.docker_host == host
        and _pool.limits == limits
    ):
        return _pool
    if _pool is not None and _pool._stale_loop():
        log.warning("sandbox pool outlived its event loop; replacing (warm orphans self-terminate)")
    _pool = DockerPool(image=image, allow_network=allow_network, limits=limits)
    return _pool


async def shutdown_docker_pool() -> None:
    """Graceful shutdown for the process-wide pool (app lifespan hook)."""
    global _pool
    if _pool is not None:
        await _pool.shutdown()
        _pool = None


SANDBOX_MODES = frozenset({"", "local", "docker"})


def validate_sandbox_mode(mode: str) -> None:
    """Raise on a PI_SANDBOX value we do not recognise. Call at startup.

    An unknown value used to fall through to LocalRunner silently, so a
    misspelling quietly turned off the sandbox: bash then ran inside the app
    process, inheriting its environment - PI_JWT_SECRET, PI_DATABASE_URL and
    PI_REDIS_URL were all readable by any registered user. Failing loudly is
    the only safe behaviour for a setting whose wrong value removes isolation.
    """
    if mode not in SANDBOX_MODES:
        raise ValueError(
            f"invalid PI_SANDBOX {mode!r}; expected one of "
            f"{'/'.join(repr(m) for m in sorted(SANDBOX_MODES))} "
            f"(note: 'docker' is the only sandboxed value - there is no 'docker-pool')"
        )


def _describe_limits(limits: SandboxLimits) -> str:
    return (
        f"mem={limits.memory or 'none'} pids={limits.pids or 'none'} "
        f"cpus={limits.cpus or 'none'} user={limits.effective_user}"
    )


def get_runner(mode: str, image: str = "python:3.12-slim", allow_network: bool = False) -> CommandRunner:
    """mode: '' | 'local' | 'docker'. Docker mode fails closed if unavailable.

    Docker transport: CLI by default; PI_DOCKER_HOST=tcp://host:2375 switches to
    the Engine REST API (no CLI needed, works against a remote daemon).

    PI_SANDBOX_POOL=0 selects the legacy cold path (fresh container per call);
    the warm pool is the default.

    Containers are capped by PI_SANDBOX_MEMORY / _PIDS / _CPUS and run as
    PI_SANDBOX_USER (default: the app's own uid:gid).
    """
    validate_sandbox_mode(mode)
    if mode == "docker":
        if os.environ.get("PI_SANDBOX_POOL", "1") != "0":
            runner = _get_pool(image, allow_network)
            transport = "api" if runner.docker_host else "cli"
            log.info(
                "bash sandbox: docker/%s pool (image=%s, network=%s, max=%d, ttl=%ds, %s)",
                transport, image, "on" if allow_network else "off",
                runner.pool_max, int(runner.idle_ttl), _describe_limits(runner.limits),
            )
            return runner
        runner = DockerRunner(image=image, allow_network=allow_network)
        transport = "api" if runner.docker_host else "cli"
        log.info(
            "bash sandbox: docker/%s cold (image=%s, network=%s, %s)",
            transport, image, "on" if allow_network else "off",
            _describe_limits(runner.limits),
        )
        return runner
    log.warning(
        "bash sandbox: DISABLED (PI_SANDBOX=%r) - bash runs inside the app process "
        "and can read its environment, including PI_JWT_SECRET and PI_DATABASE_URL",
        mode,
    )
    return LocalRunner()


def _demux_docker_logs(data: bytes) -> str:
    """Decode Docker's non-TTY multiplexed log stream (8-byte header frames)."""
    out = bytearray()
    i = 0
    while i + 8 <= len(data):
        size = int.from_bytes(data[i + 4 : i + 8], "big")
        frame = data[i + 8 : i + 8 + size]
        out.extend(frame)
        i += 8 + size
    if not out and data:  # not multiplexed (Tty mode or raw)
        out.extend(data)
    return _decode(bytes(out))


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")
