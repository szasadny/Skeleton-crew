import asyncio
import collections
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol, Self

import docker.errors
import docker.types
from docker.client import DockerClient, from_env
from docker.models.containers import Container

from skeleton_crew.domain.container_policy import check_env, check_mount

MANAGED_LABEL = "skeleton-crew.managed"
MANAGED_VALUE = "true"
TASK_LABEL = "skeleton-crew.task"
ROLE_LABEL = "skeleton-crew.role"
CONTAINER_PREFIX = "skeleton-crew-"
WORKSPACE = "/workspace"
AGENT_USER = "agent"
ROOT_USER = "root"
SANDBOX_RUNTIME = "sysbox-runc"
KILL_GRACE_S = 30
READY_FILE = "/run/skeleton-crew-ready"
STDERR_TAIL_BYTES = 64_000
DEFAULT_MAX_STDOUT_BYTES = 50_000_000
ERROR_TAIL_CHARS = 300
_LOCAL_HOSTS = ("localhost", "127.0.0.1")
_RUNNING = "running"
_CONTROLLER_TIMEOUT_MARGIN_S = 10.0
_TIMEOUT_EXIT_CODE = 124
_KILLED_EXIT_CODE = 137
_READY_TIMEOUT_S = 60.0
_READY_POLL_S = 0.2
_EXIT_CODE_POLLS = 50
_EXIT_CODE_POLL_S = 0.1

log = logging.getLogger(__name__)


class ContainerStartError(Exception):
    pass


@dataclass(frozen=True)
class Mount:
    host_path: Path
    container_path: str
    read_only: bool


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    image: str
    network: str
    mounts: list[Mount]
    env: Mapping[str, str]
    cpus: float
    memory_gb: float
    pids: int
    labels: Mapping[str, str]


@dataclass(frozen=True)
class ContainerHandle:
    id: str
    name: str


@dataclass(frozen=True)
class ContainerInfo:
    handle: ContainerHandle
    running: bool


@dataclass(frozen=True)
class SandboxTarget:
    image: str
    network: str
    proxy_url: str
    proxy_container: str
    allowlist_path: Path


@dataclass(frozen=True)
class ExecRequest:
    argv: Sequence[str]
    user: str
    cwd: str
    timeout_s: float
    env: Mapping[str, str] = field(default_factory=dict)
    stdout_path: Path | None = None
    max_stdout_bytes: int = DEFAULT_MAX_STDOUT_BYTES

    @classmethod
    def as_agent(
        cls,
        argv: Sequence[str],
        *,
        timeout_s: float,
        stdout_path: Path | None = None,
        max_stdout_bytes: int = DEFAULT_MAX_STDOUT_BYTES,
    ) -> Self:
        return cls(
            argv,
            AGENT_USER,
            WORKSPACE,
            timeout_s,
            stdout_path=stdout_path,
            max_stdout_bytes=max_stdout_bytes,
        )

    @classmethod
    def as_root(cls, argv: Sequence[str], *, timeout_s: float) -> Self:
        return cls(argv, ROOT_USER, "/", timeout_s)


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


class ContainerRuntime(Protocol):
    async def start(self, spec: ContainerSpec) -> ContainerHandle: ...

    async def exec(self, handle: ContainerHandle, request: ExecRequest) -> ExecResult: ...

    async def remove(self, handle: ContainerHandle) -> None: ...

    async def list_labeled(self, label: str, value: str) -> list[ContainerInfo]: ...

    async def remove_labeled(self, label: str, value: str) -> int: ...

    async def health(self, target: SandboxTarget) -> list[str]: ...


def proxy_env(proxy_url: str, no_proxy: Sequence[str] = ()) -> dict[str, str]:
    bypass = ",".join(dict.fromkeys([*_LOCAL_HOSTS, *no_proxy]))
    return {
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "NO_PROXY": bypass,
        "no_proxy": bypass,
    }


def error_tail(output: bytes) -> str:
    return output.decode(errors="replace").strip()[-ERROR_TAIL_CHARS:]


def check_spec(spec: ContainerSpec) -> None:
    for mount in spec.mounts:
        check_mount(str(mount.host_path), str(mount.host_path.resolve()), mount.container_path)
    check_env(spec.env)


def timeout_argv(argv: Sequence[str], timeout_s: float) -> list[str]:
    seconds = max(1, math.ceil(timeout_s))
    return ["timeout", "-s", "INT", "-k", str(KILL_GRACE_S), str(seconds), *argv]


class _Sink:
    def __init__(self, path: Path | None, max_bytes: int) -> None:
        self._remaining = max_bytes
        self._file: IO[bytes] | None = path.open("wb") if path is not None else None
        self._memory = bytearray()

    def write(self, chunk: bytes) -> None:
        part = chunk[: self._remaining]
        self._remaining -= len(part)
        if not part:
            return
        if self._file is not None:
            self._file.write(part)
        else:
            self._memory.extend(part)

    def close(self) -> bytes:
        if self._file is not None:
            self._file.close()
        return bytes(self._memory)


def _is_ready(container: Container) -> bool:
    try:
        return container.exec_run(["test", "-f", READY_FILE]).exit_code == 0
    except docker.errors.APIError:
        return False


def _lookup[T](
    kind: str,
    name: str,
    getter: Callable[[str], T],
    missing: type[docker.errors.NotFound] = docker.errors.NotFound,
) -> tuple[T | None, list[str]]:
    try:
        return getter(name), []
    except missing:
        return None, [f"{kind} {name} is missing"]
    except docker.errors.DockerException:
        return None, [f"{kind} {name} could not be inspected"]


class DockerRuntime:
    def __init__(self, client: DockerClient | None = None) -> None:
        self._client = client if client is not None else from_env()

    async def start(self, spec: ContainerSpec) -> ContainerHandle:
        check_spec(spec)
        return await asyncio.to_thread(self._start_blocking, spec)

    def _start_blocking(self, spec: ContainerSpec) -> ContainerHandle:
        mounts = [
            docker.types.Mount(
                target=m.container_path,
                source=str(m.host_path),
                type="bind",
                read_only=m.read_only,
            )
            for m in spec.mounts
        ]
        container = self._client.containers.run(
            spec.image,
            name=spec.name,
            detach=True,
            auto_remove=False,
            runtime=SANDBOX_RUNTIME,
            privileged=False,
            init=True,
            network=spec.network,
            nano_cpus=int(spec.cpus * 1_000_000_000),
            mem_limit=f"{int(spec.memory_gb * 1024)}m",
            pids_limit=spec.pids,
            security_opt=["no-new-privileges:true"],
            labels={**spec.labels, MANAGED_LABEL: MANAGED_VALUE},
            mounts=mounts,
            environment=dict(spec.env),
        )
        # Sysbox can wedge a container killed while its nested dockerd is still starting.
        problem = self._wait_ready(container)
        if problem is not None:
            try:
                self._remove_blocking(str(container.id))
            except docker.errors.DockerException:
                log.exception("could not remove container %s", spec.name)
            raise ContainerStartError(f"container {spec.name} {problem}")
        return ContainerHandle(id=str(container.id), name=spec.name)

    def _wait_ready(self, container: Container) -> str | None:
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while not _is_ready(container):
            container.reload()
            if container.status != _RUNNING:
                return f"stopped during startup ({container.status})"
            if time.monotonic() > deadline:
                return f"was not ready after {_READY_TIMEOUT_S:.0f}s"
            time.sleep(_READY_POLL_S)
        return None

    async def exec(self, handle: ContainerHandle, request: ExecRequest) -> ExecResult:
        check_env(request.env)
        cancelled = threading.Event()
        started = time.monotonic()
        job = asyncio.create_task(
            asyncio.to_thread(self._exec_blocking, handle.id, request, cancelled)
        )
        deadline = request.timeout_s + KILL_GRACE_S + _CONTROLLER_TIMEOUT_MARGIN_S
        try:
            exit_code, stdout, stderr = await asyncio.wait_for(asyncio.shield(job), deadline)
        except TimeoutError:
            cancelled.set()
            log.warning("exec in %s exceeded the controller deadline", handle.name)
            return ExecResult(-1, b"", b"", timed_out=True)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        elapsed = time.monotonic() - started
        timed_out = exit_code == _TIMEOUT_EXIT_CODE or (
            exit_code == _KILLED_EXIT_CODE and elapsed >= request.timeout_s
        )
        return ExecResult(exit_code, stdout, stderr, timed_out)

    def _exec_blocking(
        self, container_id: str, request: ExecRequest, cancelled: threading.Event
    ) -> tuple[int, bytes, bytes]:
        api = self._client.api
        exec_id: str = api.exec_create(
            container_id,
            timeout_argv(request.argv, request.timeout_s),
            stdout=True,
            stderr=True,
            environment=dict(request.env),
            workdir=request.cwd,
            user=request.user,
        )["Id"]
        stream = api.exec_start(exec_id, stream=True, demux=True)
        sink = _Sink(request.stdout_path, request.max_stdout_bytes)
        stderr_tail: collections.deque[int] = collections.deque(maxlen=STDERR_TAIL_BYTES)
        try:
            for out, err in stream:
                if cancelled.is_set():
                    break
                if out:
                    sink.write(out)
                if err:
                    stderr_tail.extend(err)
        finally:
            stdout = sink.close()
            stream.close()
            # docker-py's CancellableStream closes the raw socket but leaves the HTTP response open.
            response = getattr(stream, "_response", None)
            if response is not None:
                response.close()
        return self._exit_code(exec_id), stdout, bytes(stderr_tail)

    def _exit_code(self, exec_id: str) -> int:
        for _ in range(_EXIT_CODE_POLLS):
            info = self._client.api.exec_inspect(exec_id)
            if not info.get("Running") and info.get("ExitCode") is not None:
                return int(info["ExitCode"])
            time.sleep(_EXIT_CODE_POLL_S)
        return -1

    async def remove(self, handle: ContainerHandle) -> None:
        await asyncio.to_thread(self._remove_blocking, handle.id)

    def _remove_blocking(self, container_id: str) -> None:
        try:
            self._client.containers.get(container_id).remove(force=True, v=True)
        except docker.errors.NotFound:
            return

    def _labeled(self, label: str, value: str) -> list[Container]:
        return self._client.containers.list(all=True, filters={"label": f"{label}={value}"})

    async def list_labeled(self, label: str, value: str) -> list[ContainerInfo]:
        return await asyncio.to_thread(self._list_labeled_blocking, label, value)

    def _list_labeled_blocking(self, label: str, value: str) -> list[ContainerInfo]:
        return [
            ContainerInfo(ContainerHandle(id=str(c.id), name=str(c.name)), c.status == _RUNNING)
            for c in self._labeled(label, value)
        ]

    async def remove_labeled(self, label: str, value: str) -> int:
        return await asyncio.to_thread(self._remove_labeled_blocking, label, value)

    def _remove_labeled_blocking(self, label: str, value: str) -> int:
        removed = 0
        for container in self._labeled(label, value):
            try:
                container.remove(force=True, v=True)
            except docker.errors.NotFound:
                continue
            removed += 1
        return removed

    async def health(self, target: SandboxTarget) -> list[str]:
        return await asyncio.to_thread(self._health_blocking, target)

    def _health_blocking(self, target: SandboxTarget) -> list[str]:
        try:
            self._client.ping()
            info = self._client.info()
        except docker.errors.DockerException:
            return ["docker daemon is unreachable"]
        problems: list[str] = []
        if SANDBOX_RUNTIME not in (info.get("Runtimes") or {}):
            problems.append(f"docker runtime {SANDBOX_RUNTIME} is not installed")
        images = self._client.images.get
        problems.extend(_lookup("image", target.image, images, docker.errors.ImageNotFound)[1])
        problems.extend(self._network_problems(target.network))
        problems.extend(self._proxy_problems(target.proxy_container))
        return problems

    def _network_problems(self, network: str) -> list[str]:
        found, problems = _lookup("network", network, self._client.networks.get)
        if found is not None and found.attrs.get("Internal") is not True:
            return [f"network {network} is not internal"]
        return problems

    def _proxy_problems(self, proxy_container: str) -> list[str]:
        found, problems = _lookup("egress proxy", proxy_container, self._client.containers.get)
        if found is not None and found.status != _RUNNING:
            return [f"egress proxy {proxy_container} is not running"]
        return problems
