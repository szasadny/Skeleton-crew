import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from skeleton_crew.runtime.containers import (
    AgentContainerSpec,
    ContainerHandle,
    ExecResult,
    check_env,
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


ArgvPredicate = Callable[[Sequence[str]], bool]
ExecResponder = ExecResult | Callable[[ExecCall], ExecResult]


@dataclass
class _Entry:
    predicate: ArgvPredicate
    response: ExecResponder
    once: bool


def ok(stdout: bytes = b"") -> ExecResult:
    return ExecResult(exit_code=0, stdout_path=None, stdout=stdout, stderr=b"", timed_out=False)


def argv_starts_with(*prefix: str) -> ArgvPredicate:
    return lambda argv: tuple(argv[: len(prefix)]) == prefix


@dataclass
class FakeRuntime:
    health_problems: list[str] = field(default_factory=list)
    started: list[AgentContainerSpec] = field(default_factory=list)
    removed: list[ContainerHandle] = field(default_factory=list)
    execs: list[ExecCall] = field(default_factory=list)
    running: dict[str, ContainerHandle] = field(default_factory=dict)
    _script: list[_Entry] = field(default_factory=list)

    def script(
        self, predicate: ArgvPredicate, response: ExecResponder, *, once: bool = False
    ) -> None:
        self._script.append(_Entry(predicate, response, once))

    async def start(self, spec: AgentContainerSpec) -> ContainerHandle:
        check_spec(spec)
        self.started.append(spec)
        handle = ContainerHandle(id=f"fake-{len(self.started)}", name=spec.name)
        self.running[handle.id] = handle
        return handle

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
        if any(h.id == handle.id for h in self.removed) and handle.id not in self.running:
            raise RuntimeError(f"container {handle.name} is not running")
        call = ExecCall(handle, tuple(argv), dict(env), cwd, user, timeout_s, stdout_path)
        self.execs.append(call)
        entry = next((e for e in self._script if e.predicate(call.argv)), None)
        if entry is None:
            raise LookupError(f"no scripted exec result for {call.argv!r}")
        if entry.once:
            self._script.remove(entry)
        result = entry.response(call) if callable(entry.response) else entry.response
        if stdout_path is None:
            return result
        await asyncio.to_thread(stdout_path.write_bytes, result.stdout[:max_stdout_bytes])
        return ExecResult(result.exit_code, stdout_path, b"", result.stderr, result.timed_out)

    async def remove(self, handle: ContainerHandle) -> None:
        self.removed.append(handle)
        self.running.pop(handle.id, None)

    async def remove_all_managed(self) -> int:
        handles = list(self.running.values())
        for handle in handles:
            await self.remove(handle)
        return len(handles)

    async def health(self, *, image: str, network: str, proxy_container: str) -> list[str]:
        return list(self.health_problems)
