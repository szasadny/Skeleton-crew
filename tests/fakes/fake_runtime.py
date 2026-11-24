import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from skeleton_crew.domain.container_policy import check_env
from skeleton_crew.runtime.containers import (
    MANAGED_LABEL,
    MANAGED_VALUE,
    ContainerHandle,
    ContainerInfo,
    ContainerSpec,
    ExecRequest,
    ExecResult,
    SandboxTarget,
    check_spec,
)


@dataclass(frozen=True)
class ExecCall:
    handle: ContainerHandle
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str
    user: str
    timeout_s: float
    stdout_path: Path | None
    max_stdout_bytes: int


ArgvPredicate = Callable[[Sequence[str]], bool]
ExecResponder = ExecResult | Callable[[ExecCall], ExecResult]


@dataclass
class _Entry:
    predicate: ArgvPredicate
    response: ExecResponder
    once: bool


def exec_result(
    exit_code: int = 0, stdout: bytes = b"", stderr: bytes = b"", *, timed_out: bool = False
) -> ExecResult:
    return ExecResult(exit_code, stdout, stderr, timed_out)


def argv_starts_with(*prefix: str) -> ArgvPredicate:
    return lambda argv: tuple(argv[: len(prefix)]) == prefix


@dataclass
class FakeRuntime:
    health_problems: list[str] = field(default_factory=list)
    started: list[ContainerSpec] = field(default_factory=list)
    removed: list[ContainerHandle] = field(default_factory=list)
    execs: list[ExecCall] = field(default_factory=list)
    running: dict[str, ContainerHandle] = field(default_factory=dict)
    labels: dict[str, Mapping[str, str]] = field(default_factory=dict)
    exited: set[str] = field(default_factory=set)
    _script: list[_Entry] = field(default_factory=list)

    def script(
        self, predicate: ArgvPredicate, response: ExecResponder, *, once: bool = False
    ) -> None:
        self._script.append(_Entry(predicate, response, once))

    async def start(self, spec: ContainerSpec) -> ContainerHandle:
        check_spec(spec)
        self.started.append(spec)
        handle = ContainerHandle(id=f"fake-{len(self.started)}", name=spec.name)
        self.running[handle.id] = handle
        self.labels[handle.id] = {**spec.labels, MANAGED_LABEL: MANAGED_VALUE}
        return handle

    async def exec(self, handle: ContainerHandle, request: ExecRequest) -> ExecResult:
        check_env(request.env)
        if any(h.id == handle.id for h in self.removed) and handle.id not in self.running:
            raise RuntimeError(f"container {handle.name} is not running")
        call = ExecCall(
            handle,
            tuple(request.argv),
            dict(request.env),
            request.cwd,
            request.user,
            request.timeout_s,
            request.stdout_path,
            request.max_stdout_bytes,
        )
        self.execs.append(call)
        entry = next((e for e in self._script if e.predicate(call.argv)), None)
        if entry is None:
            raise LookupError(f"no scripted exec result for {call.argv!r}")
        if entry.once:
            self._script.remove(entry)
        result = entry.response(call) if callable(entry.response) else entry.response
        stdout = result.stdout[: request.max_stdout_bytes]
        if request.stdout_path is None:
            return ExecResult(result.exit_code, stdout, result.stderr, result.timed_out)
        await asyncio.to_thread(request.stdout_path.write_bytes, stdout)
        return ExecResult(result.exit_code, b"", result.stderr, result.timed_out)

    async def remove(self, handle: ContainerHandle) -> None:
        self.removed.append(handle)
        self.running.pop(handle.id, None)

    async def list_labeled(self, label: str, value: str) -> list[ContainerInfo]:
        return [
            ContainerInfo(handle, handle.id not in self.exited)
            for handle in self.running.values()
            if self.labels.get(handle.id, {}).get(label) == value
        ]

    async def remove_labeled(self, label: str, value: str) -> int:
        infos = await self.list_labeled(label, value)
        for info in infos:
            await self.remove(info.handle)
        return len(infos)

    def exit(self, handle: ContainerHandle) -> None:
        self.exited.add(handle.id)

    async def health(self, target: SandboxTarget) -> list[str]:
        return list(self.health_problems)
