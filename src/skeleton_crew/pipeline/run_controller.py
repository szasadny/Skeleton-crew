import asyncio
from collections.abc import Iterator
from contextlib import contextmanager, suppress

from skeleton_crew.core.audit import AuditEntry, AuditKind, AuditOutcome
from skeleton_crew.domain.task_state import RunStep, RunTrigger
from skeleton_crew.pipeline.deps import PipelineDeps, audit_sink
from skeleton_crew.pipeline.recovery import (
    KILLED_NOTE,
    RESTART_NOTE,
    abort_leftovers,
    comment_interrupted,
    remove_managed_containers,
)
from skeleton_crew.pipeline.run_flow import RunStart, execute_run
from skeleton_crew.pipeline.runner_pool import RunnerPool
from skeleton_crew.store import runs_store, settings_store


class RunController:
    def __init__(self, deps: PipelineDeps, runners: RunnerPool | None = None) -> None:
        self._deps = deps
        self._runners = runners
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._step: RunStep | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def step(self) -> RunStep | None:
        return self._step if self.is_running else None

    async def start(self, trigger: RunTrigger) -> int | None:
        async with self._lock:
            if self.is_running:
                return None
            async with self._deps.sessionmaker() as session:
                settings = await settings_store.load_settings(session)
                if settings.kill_switch:
                    return None
                run = await runs_store.create_run(session, trigger, settings.dry_run)
                await session.commit()
            sink = audit_sink(self._deps.sessionmaker, run.id)
            await sink(
                AuditEntry(AuditKind.RUN_STARTED, AuditOutcome.INFO, f"run {run.id} started")
            )
            start = RunStart(run.id, settings, sink)
            self._task = asyncio.create_task(execute_run(self._deps, start, self._in_step))
            return run.id

    async def wait(self) -> None:
        if self._task is not None:
            with suppress(asyncio.CancelledError):
                await self._task

    async def kill(self) -> None:
        async with self._lock:
            task = self._task
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if self._runners is not None:
                await self._runners.reset()
            await remove_managed_containers(self._deps)
            await abort_leftovers(self._deps, KILLED_NOTE)

    async def reconcile_on_startup(self) -> None:
        async with self._lock:
            await remove_managed_containers(self._deps)
            interrupted = await abort_leftovers(self._deps, RESTART_NOTE)
            if interrupted:
                await comment_interrupted(self._deps, interrupted)

    @contextmanager
    def _in_step(self, step: RunStep) -> Iterator[None]:
        self._step = step
        try:
            yield
        finally:
            self._step = None
