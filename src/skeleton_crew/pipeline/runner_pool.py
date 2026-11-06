import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from skeleton_crew.domain.runner_policy import (
    RunnerState,
    RunnerStatus,
    eligibility_problem,
    is_crash,
    restart_delay,
    runner_names,
)
from skeleton_crew.domain.settings import AppSettings
from skeleton_crew.github.gateway import Gateway
from skeleton_crew.pipeline.deps import PipelineDeps, audit_sink
from skeleton_crew.pipeline.sessions import ROLE_LABEL, proxy_env
from skeleton_crew.runtime.containers import (
    AgentContainerSpec,
    ContainerHandle,
    ContainerInfo,
    ContainerStartError,
    Mount,
)
from skeleton_crew.runtime.egress import apply_allowlist
from skeleton_crew.store import settings_store

RUNNER_ROLE = "runner"
CONTAINER_PREFIX = "skeleton-crew-"
CONFIG_MOUNT = "/runner-config"
REGISTRATION_FILE = ".runner"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
# By process name: the exec's own timeout wrapper has "Runner.Worker" in its command line.
BUSY_ARGV = ("pgrep", "-x", "Runner.Worker")
TICK_S = 5.0
ELIGIBILITY_TTL_S = 600.0
_EXEC_TIMEOUT_S = 10.0
_NOT_FOUND_EXIT = 1

log = logging.getLogger(__name__)


@dataclass
class _Slot:
    started_at: float = 0.0
    failures: int = 0
    not_before: float = 0.0
    reason: str = ""


def container_name(runner: str) -> str:
    return f"{CONTAINER_PREFIX}{runner}"


def config_dir(deps: PipelineDeps, runner: str) -> Path:
    return deps.config.runners_dir / runner


def registered_runners(deps: PipelineDeps, settings: AppSettings) -> list[str]:
    names = runner_names(settings.ci.runners)
    return [n for n in names if (config_dir(deps, n) / REGISTRATION_FILE).is_file()]


def runner_env(proxy_url: str, no_proxy: Sequence[str]) -> dict[str, str]:
    bypass = ",".join(dict.fromkeys([*LOCAL_HOSTS, *no_proxy]))
    return proxy_env(proxy_url) | {"NO_PROXY": bypass, "no_proxy": bypass}


def runner_spec(deps: PipelineDeps, settings: AppSettings, runner: str) -> AgentContainerSpec:
    config, ci = deps.config, settings.ci
    return AgentContainerSpec(
        name=container_name(runner),
        image=config.runner_image,
        network=config.runner_network,
        # Read-only, so a job cannot tamper with the registration the next job starts from.
        mounts=[Mount(config_dir(deps, runner), CONFIG_MOUNT, read_only=True)],
        env=runner_env(config.runner_proxy_url, ci.no_proxy),
        cpus=ci.cpus,
        memory_gb=ci.memory_gb,
        pids=ci.pids,
        labels={ROLE_LABEL: RUNNER_ROLE},
    )


async def visibility_problem(gw: Gateway) -> str | None:
    try:
        private = await gw.repo_is_private()
    except Exception as exc:
        return f"cannot read the repository visibility: {type(exc).__name__}"
    return eligibility_problem(private=private)


async def runner_problems(deps: PipelineDeps, settings: AppSettings, gw: Gateway) -> list[str]:
    if not settings.ci.enabled:
        return []
    config = deps.config
    problems = await deps.runtime.health(
        image=config.runner_image,
        network=config.runner_network,
        proxy_container=config.runner_proxy_container,
    )
    problem = await visibility_problem(gw)
    if problem is not None:
        problems.append(problem)
    if not registered_runners(deps, settings):
        problems.append("self-hosted CI is on, but no runner is registered")
    return problems


class RunnerPool:
    def __init__(self, deps: PipelineDeps, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._deps = deps
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._slots: dict[str, _Slot] = {}
        self._states: list[RunnerState] = []
        self._eligibility: tuple[float, str | None] | None = None
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

    async def stop(self) -> None:
        restart = self._task is not None
        await self.close()
        await self._remove_all()
        self._states = [RunnerState(s.name, RunnerStatus.OFFLINE, "stopped") for s in self._states]
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
            async with self._deps.sessionmaker() as session:
                settings = await settings_store.load_settings(session)
            names = runner_names(settings.ci.runners)
            blocker = await self._blocker(settings)
            if blocker is not None:
                await self._remove_all()
                self._slots.clear()
                shown = names if settings.ci.enabled else []
                self._states = [RunnerState(n, RunnerStatus.OFFLINE, blocker) for n in shown]
                return
            await self._apply_allowlist(settings)
            containers = {
                info.handle.name: info
                for info in await self._deps.runtime.list_labeled(ROLE_LABEL, RUNNER_ROLE)
            }
            wanted = {container_name(n) for n in names}
            for name, info in containers.items():
                if name not in wanted:
                    await self._deps.runtime.remove(info.handle)
            self._states = [
                await self._reconcile(settings, n, containers.get(container_name(n))) for n in names
            ]

    async def _blocker(self, settings: AppSettings) -> str | None:
        if not settings.ci.enabled:
            return "self-hosted CI is off"
        if settings.kill_switch:
            return "kill switch is engaged"
        now = self._monotonic()
        if self._eligibility is not None and now - self._eligibility[0] <= ELIGIBILITY_TTL_S:
            return self._eligibility[1]
        gw = self._deps.gateway_factory(settings, audit_sink(self._deps.sessionmaker, None))
        try:
            private = await gw.repo_is_private()
        except Exception as exc:
            self._eligibility = None
            return f"cannot read the repository visibility: {type(exc).__name__}"
        self._eligibility = (now, eligibility_problem(private=private))
        return self._eligibility[1]

    async def _apply_allowlist(self, settings: AppSettings) -> None:
        domains = frozenset(settings.ci.egress_allowlist)
        if domains == self._allowlist:
            return
        config = self._deps.config
        await apply_allowlist(
            self._deps.runtime, domains, config.runner_allowlist_path, config.runner_proxy_container
        )
        self._allowlist = domains

    async def _reconcile(
        self, settings: AppSettings, runner: str, info: ContainerInfo | None
    ) -> RunnerState:
        slot = self._slots.setdefault(runner, _Slot())
        now = self._monotonic()
        if not (config_dir(self._deps, runner) / REGISTRATION_FILE).is_file():
            if info is not None:
                await self._deps.runtime.remove(info.handle)
            self._slots[runner] = _Slot()
            return RunnerState(runner, RunnerStatus.NOT_REGISTERED)
        if info is not None and info.running:
            return RunnerState(runner, await self._activity(info.handle))
        if info is not None:
            await self._deps.runtime.remove(info.handle)
            if is_crash(now - slot.started_at):
                slot.failures += 1
                slot.reason = "the runner stopped right after starting; check its registration"
            else:
                slot.failures = 0
            slot.not_before = now + restart_delay(slot.failures)
        if now < slot.not_before:
            wait = slot.not_before - now
            return RunnerState(
                runner, RunnerStatus.OFFLINE, f"{slot.reason} (retry in {wait:.0f}s)"
            )
        slot.started_at = now
        try:
            await self._deps.runtime.start(runner_spec(self._deps, settings, runner))
        except ContainerStartError as exc:
            slot.failures += 1
            slot.not_before = now + restart_delay(slot.failures)
            slot.reason = str(exc)
            return RunnerState(runner, RunnerStatus.OFFLINE, slot.reason)
        return RunnerState(runner, RunnerStatus.STARTING)

    async def _activity(self, handle: ContainerHandle) -> RunnerStatus:
        try:
            result = await self._deps.runtime.exec(
                handle, BUSY_ARGV, env={}, cwd="/", user="root", timeout_s=_EXEC_TIMEOUT_S
            )
        except Exception:
            return RunnerStatus.OFFLINE
        if result.exit_code == 0:
            return RunnerStatus.BUSY
        return RunnerStatus.IDLE if result.exit_code == _NOT_FOUND_EXIT else RunnerStatus.OFFLINE

    async def _remove_all(self) -> None:
        try:
            await self._deps.runtime.remove_labeled(ROLE_LABEL, RUNNER_ROLE)
        except Exception:
            log.exception("could not remove runner containers")
