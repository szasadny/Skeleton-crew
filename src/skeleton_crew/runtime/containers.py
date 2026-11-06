import asyncio
import collections
import logging
import math
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

import docker.errors
import docker.types
from docker.client import DockerClient, from_env
from docker.models.containers import Container

from skeleton_crew.domain.errors import PolicyViolation

MANAGED_LABEL = "skeleton-crew.managed"
SANDBOX_RUNTIME = "sysbox-runc"
KILL_GRACE_S = 30
READY_FILE = "/run/skeleton-crew-ready"
STDERR_TAIL_BYTES = 64_000
_CONTROLLER_TIMEOUT_MARGIN_S = 10.0
_TIMEOUT_EXIT_CODE = 124
_KILLED_EXIT_CODE = 137
_READY_TIMEOUT_S = 60.0
_READY_POLL_S = 0.2
_DOCKER_SOCKET_PATHS = frozenset({"/var/run/docker.sock", "/run/docker.sock"})
_GITHUB_TOKEN_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")

log = logging.getLogger(__name__)


class ContainerStartError(Exception):
    pass


@dataclass(frozen=True)
class Mount:
    host_path: Path
    container_path: str
    read_only: bool


@dataclass(frozen=True)
class AgentContainerSpec:
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
class ExecResult:
    exit_code: int
    stdout_path: Path | None
    stdout: bytes
    stderr: bytes
    timed_out: bool


class ContainerRuntime(Protocol):
    async def start(self, spec: AgentContainerSpec) -> ContainerHandle: ...

    async def exec(
        self,
        handle: ContainerHandle,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        user: str,
        timeout_s: float,
        stdout_path: Path | None = None,
        max_stdout_bytes: int = 50_000_000,
    ) -> ExecResult: ...

    async def remove(self, handle: ContainerHandle) -> None: ...

    async def remove_all_managed(self) -> int: ...

    async def list_labeled(self, label: str, value: str) -> list[ContainerInfo]: ...

    async def remove_labeled(self, label: str, value: str) -> int: ...

    async def health(self, *, image: str, network: str, proxy_container: str) -> list[str]: ...


def _is_github_credential(key: str, value: str) -> bool:
    upper = key.upper()
    if "TOKEN" in upper and ("GITHUB" in upper or upper.startswith("GH_")):
        return True
    if upper.startswith("GIT_CONFIG_"):
        return True
    lowered = value.lower()
    return value.startswith(_GITHUB_TOKEN_PREFIXES) or "x-access-token" in lowered


def check_env(env: Mapping[str, str]) -> None:
    for key, value in env.items():
        if _is_github_credential(key, value):
            raise PolicyViolation(f"GitHub credential in container env: {key}")


def _is_docker_socket(path: str) -> bool:
    return Path(path).name == "docker.sock" or path in _DOCKER_SOCKET_PATHS


def check_spec(spec: AgentContainerSpec) -> None:
    for mount in spec.mounts:
        resolved = str(mount.host_path.resolve())
        if _is_docker_socket(str(mount.host_path)) or _is_docker_socket(resolved):
            raise PolicyViolation("docker socket must never be mounted into a container")
        if _is_docker_socket(mount.container_path):
            raise PolicyViolation("docker socket path must never be a mount target")
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


class DockerRuntime:
    def __init__(self, client: DockerClient | None = None) -> None:
        self._client = client if client is not None else from_env()

    async def start(self, spec: AgentContainerSpec) -> ContainerHandle:
        check_spec(spec)
        return await asyncio.to_thread(self._start_blocking, spec)

    def _start_blocking(self, spec: AgentContainerSpec) -> ContainerHandle:
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
            labels={**spec.labels, MANAGED_LABEL: "true"},
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
            if container.status != "running":
                return f"stopped during startup ({container.status})"
            if time.monotonic() > deadline:
                return f"was not ready after {_READY_TIMEOUT_S:.0f}s"
            time.sleep(_READY_POLL_S)
        return None

    async def exec(
        self,
        handle: ContainerHandle,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        user: str,
        timeout_s: float,
        stdout_path: Path | None = None,
        max_stdout_bytes: int = 50_000_000,
    ) -> ExecResult:
        check_env(env)
        cancelled = threading.Event()
        started = time.monotonic()
        job = asyncio.create_task(
            asyncio.to_thread(
                self._exec_blocking,
                handle.id,
                timeout_argv(argv, timeout_s),
                env,
                cwd,
                user,
                stdout_path,
                max_stdout_bytes,
                cancelled,
            )
        )
        deadline = timeout_s + KILL_GRACE_S + _CONTROLLER_TIMEOUT_MARGIN_S
        try:
            exit_code, stdout, stderr = await asyncio.wait_for(asyncio.shield(job), deadline)
        except TimeoutError:
            cancelled.set()
            log.warning("exec in %s exceeded the controller deadline", handle.name)
            return ExecResult(-1, stdout_path, b"", b"", timed_out=True)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        elapsed = time.monotonic() - started
        timed_out = exit_code == _TIMEOUT_EXIT_CODE or (
            exit_code == _KILLED_EXIT_CODE and elapsed >= timeout_s
        )
        return ExecResult(exit_code, stdout_path, stdout, stderr, timed_out)

    def _exec_blocking(
        self,
        container_id: str,
        argv: list[str],
        env: Mapping[str, str],
        cwd: str,
        user: str,
        stdout_path: Path | None,
        max_stdout_bytes: int,
        cancelled: threading.Event,
    ) -> tuple[int, bytes, bytes]:
        api = self._client.api
        exec_id: str = api.exec_create(
            container_id,
            argv,
            stdout=True,
            stderr=True,
            environment=dict(env),
            workdir=cwd,
            user=user,
        )["Id"]
        stream = api.exec_start(exec_id, stream=True, demux=True)
        sink = _Sink(stdout_path, max_stdout_bytes)
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
        for _ in range(50):
            info = self._client.api.exec_inspect(exec_id)
            if not info.get("Running") and info.get("ExitCode") is not None:
                return int(info["ExitCode"])
            time.sleep(0.1)
        return -1

    async def remove(self, handle: ContainerHandle) -> None:
        await asyncio.to_thread(self._remove_blocking, handle.id)

    def _remove_blocking(self, container_id: str) -> None:
        try:
            self._client.containers.get(container_id).remove(force=True, v=True)
        except docker.errors.NotFound:
            return

    async def remove_all_managed(self) -> int:
        return await asyncio.to_thread(self._remove_all_blocking)

    def _remove_all_blocking(self) -> int:
        return self._remove_labeled_blocking(MANAGED_LABEL, "true")

    async def list_labeled(self, label: str, value: str) -> list[ContainerInfo]:
        return await asyncio.to_thread(self._list_labeled_blocking, label, value)

    def _list_labeled_blocking(self, label: str, value: str) -> list[ContainerInfo]:
        containers = self._client.containers.list(all=True, filters={"label": f"{label}={value}"})
        return [
            ContainerInfo(ContainerHandle(id=str(c.id), name=str(c.name)), c.status == "running")
            for c in containers
        ]

    async def remove_labeled(self, label: str, value: str) -> int:
        return await asyncio.to_thread(self._remove_labeled_blocking, label, value)

    def _remove_labeled_blocking(self, label: str, value: str) -> int:
        containers = self._client.containers.list(all=True, filters={"label": f"{label}={value}"})
        removed = 0
        for container in containers:
            try:
                container.remove(force=True, v=True)
            except docker.errors.NotFound:
                continue
            removed += 1
        return removed

    async def health(self, *, image: str, network: str, proxy_container: str) -> list[str]:
        return await asyncio.to_thread(self._health_blocking, image, network, proxy_container)

    def _health_blocking(self, image: str, network: str, proxy_container: str) -> list[str]:
        try:
            self._client.ping()
            info = self._client.info()
        except docker.errors.DockerException:
            return ["docker daemon is unreachable"]
        problems: list[str] = []
        if SANDBOX_RUNTIME not in (info.get("Runtimes") or {}):
            problems.append(f"docker runtime {SANDBOX_RUNTIME} is not installed")
        problems.extend(self._image_problems(image))
        problems.extend(self._network_problems(network))
        problems.extend(self._proxy_problems(proxy_container))
        return problems

    def _image_problems(self, image: str) -> list[str]:
        try:
            self._client.images.get(image)
        except docker.errors.ImageNotFound:
            return [f"image {image} is missing"]
        except docker.errors.DockerException:
            return [f"image {image} could not be inspected"]
        return []

    def _network_problems(self, network: str) -> list[str]:
        try:
            attrs = self._client.networks.get(network).attrs
        except docker.errors.NotFound:
            return [f"network {network} is missing"]
        except docker.errors.DockerException:
            return [f"network {network} could not be inspected"]
        if attrs.get("Internal") is not True:
            return [f"network {network} is not internal"]
        return []

    def _proxy_problems(self, proxy_container: str) -> list[str]:
        try:
            status = self._client.containers.get(proxy_container).status
        except docker.errors.NotFound:
            return [f"egress proxy {proxy_container} is missing"]
        except docker.errors.DockerException:
            return [f"egress proxy {proxy_container} could not be inspected"]
        if status != "running":
            return [f"egress proxy {proxy_container} is not running"]
        return []
