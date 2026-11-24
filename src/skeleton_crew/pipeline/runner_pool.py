import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from skeleton_crew.core.config import BootstrapConfig
from skeleton_crew.domain.branch_policy import BRANCH_PREFIX
from skeleton_crew.domain.runner_policy import (
    DRAINING_REASON,
    NO_RUNNER_REGISTERED,
    STOPPED_REASON,
    RunnerSlot,
    RunnerState,
    RunnerStatus,
    after_exit,
    after_start_failure,
    eligibility_problem,
    is_definite,
    retry_reason,
    runner_names,
    settings_blocker,
    standby_state,
    unreadable_visibility,
)
from skeleton_crew.domain.settings import AppSettings
from skeleton_crew.github.gateway import Gateway
from skeleton_crew.pipeline.deps import PipelineDeps
from skeleton_crew.runtime.containers import (
    CONTAINER_PREFIX,
    ROLE_LABEL,
    ContainerHandle,
    ContainerInfo,
    ContainerSpec,
    ContainerStartError,
    ExecRequest,
    Mount,
    SandboxTarget,
    proxy_env,
)
from skeleton_crew.runtime.egress import apply_allowlist
from skeleton_crew.store import runs_store

RUNNER_ROLE = "runner"
CONFIG_MOUNT = "/runner-config"
REGISTRATION_FILE = ".runner"
_BRANCH_PREFIX_ENV = "SC_BRANCH_PREFIX"
_EXTRA_LOCAL_HOSTS = ("::1",)
# By process name: the exec's own timeout wrapper has "Runner.Worker" in its command line.
BUSY_ARGV = ("pgrep", "-x", "Runner.Worker")
TICK_S = 5.0
ELIGIBILITY_TTL_S = 600.0
CI_DEMAND_TTL_S = 30.0
_EXEC_TIMEOUT_S = 10.0
_FOUND_EXIT = 0
_NOT_FOUND_EXIT = 1

log = logging.getLogger(__name__)


class _TtlValue[T]:
    def __init__(self, ttl_s: float, monotonic: Callable[[], float]) -> None:
        self._ttl_s = ttl_s
        self._monotonic = monotonic
        self._entry: tuple[float, T] | None = None

    async def get(self, load: Callable[[], Awaitable[tuple[T, bool]]]) -> T:
        now = self._monotonic()
        if self._entry is not None and now - self._entry[0] <= self._ttl_s:
            return self._entry[1]
        value, keep = await load()
        self._entry = (now, value) if keep else None
        return value


def container_name(runner: str) -> str:
    return f"{CONTAINER_PREFIX}{runner}"


def config_dir(deps: PipelineDeps, runner: str) -> Path:
    return deps.config.runners_dir / runner


def _is_registered(deps: PipelineDeps, runner: str) -> bool:
    return (config_dir(deps, runner) / REGISTRATION_FILE).is_file()


def registered_runners(deps: PipelineDeps, settings: AppSettings) -> list[str]:
    return [n for n in runner_names(settings.ci.runners) if _is_registered(deps, n)]


def runner_target(config: BootstrapConfig) -> SandboxTarget:
    return SandboxTarget(
        image=config.runner_image,
        network=config.runner_network,
        proxy_url=config.runner_proxy_url,
        proxy_container=config.runner_proxy_container,
        allowlist_path=config.runner_allowlist_path,
    )


def runner_env(proxy_url: str, no_proxy: Sequence[str]) -> dict[str, str]:
    env = proxy_env(proxy_url, (*_EXTRA_LOCAL_HOSTS, *no_proxy))
    return env | {_BRANCH_PREFIX_ENV: BRANCH_PREFIX}


def runner_spec(deps: PipelineDeps, settings: AppSettings, runner: str) -> ContainerSpec:
    target, ci = runner_target(deps.config), settings.ci
    return ContainerSpec(
        name=container_name(runner),
        image=target.image,
        network=target.network,
        # Read-only, so a job cannot tamper with the registration the next job starts from.
        mounts=[Mount(config_dir(deps, runner), CONFIG_MOUNT, read_only=True)],
        env=runner_env(target.proxy_url, ci.no_proxy),
        cpus=ci.cpus,
        memory_gb=ci.memory_gb,
        pids=ci.pids,
        labels={ROLE_LABEL: RUNNER_ROLE},
    )


async def visibility_problem(gw: Gateway) -> str | None:
    try:
        private = await gw.repo_is_private()
    except Exception as exc:
        return unreadable_visibility(type(exc).__name__)
    return eligibility_problem(private=private)


async def runner_problems(deps: PipelineDeps, settings: AppSettings, gw: Gateway) -> list[str]:
    if not settings.ci.enabled:
        return []
    problems = await deps.runtime.health(runner_target(deps.config))
    problem = await visibility_problem(gw)
    if problem is not None:
        problems.append(problem)
    if not registered_runners(deps, settings):
        problems.append(NO_RUNNER_REGISTERED)
    return problems


class RunnerPool:
    def __init__(self, deps: PipelineDeps, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._deps = deps
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._slots: dict[str, RunnerSlot] = {}
        self._states: list[RunnerState] = []
        self._eligibility = _TtlValue[str | None](ELIGIBILITY_TTL_S, monotonic)
        self._demand = _TtlValue[bool](CI_DEMAND_TTL_S, monotonic)
        self._allowlist: frozenset[str] | None = None
        self._task: asyncio.Task[None] | None = None

    def statuses(self) -> list[RunnerState]:
        return list(self._states)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def reset(self) -> None:
        restart = self._task is not None
        await self.close()
        await self._remove_all()
        self._states = [
            RunnerState(s.name, RunnerStatus.OFFLINE, STOPPED_REASON) for s in self._states
        ]
        if restart:
            self.start()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("runner reconcile failed")
            await asyncio.sleep(TICK_S)

    async def tick(self) -> None:
        async with self._lock:
            settings = await self._deps.load_settings()
            async with self._deps.sessionmaker() as session:
                waiting = await runs_store.awaiting_ci_count(session)
            names = runner_names(settings.ci.runners)
            blocker = await self._blocker(settings)
            if blocker is not None:
                shown = names if settings.ci.enabled else []
                await self._park([RunnerState(n, RunnerStatus.OFFLINE, blocker) for n in shown])
            elif waiting or await self._github_demand(settings):
                await self._apply_allowlist(settings)
                await self._run(settings, names)
            else:
                await self._drain(settings, names)

    async def _park(self, states: list[RunnerState]) -> None:
        await self._remove_all()
        self._slots.clear()
        self._states = states

    async def _drain(self, settings: AppSettings, names: list[str]) -> None:
        registered = registered_runners(self._deps, settings)
        containers = await self._runner_containers()
        states: list[RunnerState] = []
        for name in names:
            info = containers.pop(container_name(name), None)
            if info is not None and info.running and await self._is_busy(info.handle):
                states.append(RunnerState(name, RunnerStatus.BUSY, DRAINING_REASON))
                continue
            await self._remove(info)
            self._slots.pop(name, None)
            states.append(standby_state(name, registered=name in registered))
        for info in containers.values():
            await self._remove(info)
        self._states = states

    async def _run(self, settings: AppSettings, names: list[str]) -> None:
        containers = await self._runner_containers()
        wanted = {container_name(n) for n in names}
        for name, info in containers.items():
            if name not in wanted:
                await self._remove(info)
        self._states = [
            await self._reconcile(settings, n, containers.get(container_name(n))) for n in names
        ]

    async def _runner_containers(self) -> dict[str, ContainerInfo]:
        listed = await self._deps.runtime.list_labeled(ROLE_LABEL, RUNNER_ROLE)
        return {info.handle.name: info for info in listed}

    async def _blocker(self, settings: AppSettings) -> str | None:
        blocker = settings_blocker(settings)
        if blocker is not None:
            return blocker
        return await self._eligibility.get(lambda: self._read_visibility(settings))

    async def _read_visibility(self, settings: AppSettings) -> tuple[str | None, bool]:
        problem = await visibility_problem(self._deps.gateway(settings))
        # Only a definite answer is cached; a failed read is retried on the next tick.
        return problem, is_definite(problem)

    async def _github_demand(self, settings: AppSettings) -> bool:
        return await self._demand.get(lambda: self._read_demand(settings))

    async def _read_demand(self, settings: AppSettings) -> tuple[bool, bool]:
        try:
            return await self._deps.gateway(settings).skeleton_ci_active(), True
        except Exception:
            log.exception("could not read the CI runs of Skeleton Crew branches")
            return False, False

    async def _apply_allowlist(self, settings: AppSettings) -> None:
        domains = frozenset(settings.ci.egress_allowlist)
        if domains == self._allowlist:
            return
        await apply_allowlist(self._deps.runtime, domains, runner_target(self._deps.config))
        self._allowlist = domains

    async def _reconcile(
        self, settings: AppSettings, runner: str, info: ContainerInfo | None
    ) -> RunnerState:
        now = self._monotonic()
        if not _is_registered(self._deps, runner):
            await self._remove(info)
            self._slots.pop(runner, None)
            return RunnerState(runner, RunnerStatus.NOT_REGISTERED)
        if info is not None and info.running:
            return RunnerState(runner, await self._activity(info.handle))
        slot = self._slots.get(runner, RunnerSlot())
        if info is not None:
            await self._remove(info)
            slot = after_exit(slot, now)
        waiting = retry_reason(slot, now)
        if waiting is not None:
            self._slots[runner] = slot
            return RunnerState(runner, RunnerStatus.OFFLINE, waiting)
        self._slots[runner] = replace(slot, started_at=now)
        return await self._start(settings, runner, now)

    async def _start(self, settings: AppSettings, runner: str, now: float) -> RunnerState:
        try:
            await self._deps.runtime.start(runner_spec(self._deps, settings, runner))
        except ContainerStartError as exc:
            self._slots[runner] = after_start_failure(self._slots[runner], now, str(exc))
            return RunnerState(runner, RunnerStatus.OFFLINE, str(exc))
        return RunnerState(runner, RunnerStatus.STARTING)

    async def _activity(self, handle: ContainerHandle) -> RunnerStatus:
        try:
            request = ExecRequest.as_root(BUSY_ARGV, timeout_s=_EXEC_TIMEOUT_S)
            result = await self._deps.runtime.exec(handle, request)
        except Exception:
            return RunnerStatus.OFFLINE
        if result.exit_code == _FOUND_EXIT:
            return RunnerStatus.BUSY
        return RunnerStatus.IDLE if result.exit_code == _NOT_FOUND_EXIT else RunnerStatus.OFFLINE

    async def _is_busy(self, handle: ContainerHandle) -> bool:
        return await self._activity(handle) is RunnerStatus.BUSY

    async def _remove(self, info: ContainerInfo | None) -> None:
        if info is not None:
            await self._deps.runtime.remove(info.handle)

    async def _remove_all(self) -> None:
        try:
            await self._deps.runtime.remove_labeled(ROLE_LABEL, RUNNER_ROLE)
        except Exception:
            log.exception("could not remove runner containers")
