import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from datetime import timedelta
from typing import Literal

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.settings import AppSettings
from skeleton_crew.domain.task_state import (
    RefineState,
    RunState,
    TaskEvent,
    TaskKind,
    TaskState,
    transition,
)
from skeleton_crew.domain.trust_policy import IssueComment
from skeleton_crew.github.gateway import IssueRef
from skeleton_crew.github.write_guard import AuditSink
from skeleton_crew.pipeline.deps import PipelineDeps, RunContext, audit_sink
from skeleton_crew.pipeline.escalation import escalate_issue
from skeleton_crew.pipeline.pr_maintenance import FixBudget, PrUpdate, run_pr_update
from skeleton_crew.pipeline.refine_flow import run_refine
from skeleton_crew.pipeline.run_setup import (
    Candidate,
    OpenPr,
    apply_egress,
    find_candidates,
    load_rules,
    make_backends,
    preflight_problems,
    remaining_quotas,
)
from skeleton_crew.pipeline.selector import select_issues
from skeleton_crew.pipeline.texts import INTERRUPTED_COMMENT
from skeleton_crew.pipeline.worker_flow import run_worker
from skeleton_crew.store import runs_store, settings_store

KILLED_NOTE = "killed"
RESTART_NOTE = "interrupted by a controller restart"
NOTE_CHARS = 1000

log = logging.getLogger(__name__)

type Outcome = tuple[RunState, str | None]


class RunController:
    def __init__(self, deps: PipelineDeps) -> None:
        self._deps = deps
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, trigger: Literal["schedule", "manual"]) -> int | None:
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
            await sink(AuditKind.RUN_STARTED, AuditOutcome.INFO, f"run {run.id} started", None)
            self._task = asyncio.create_task(self._execute(run.id, settings, sink))
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
            await self._remove_containers()
            await self._abort_leftovers(KILLED_NOTE)

    async def reconcile_on_startup(self) -> None:
        async with self._lock:
            await self._remove_containers()
            interrupted = await self._abort_leftovers(RESTART_NOTE)
            if interrupted:
                await self._comment_interrupted(interrupted)

    async def _remove_containers(self) -> None:
        try:
            await self._deps.runtime.remove_all_managed()
        except Exception:
            log.exception("could not remove managed containers")

    async def _abort_leftovers(self, note: str) -> list[int]:
        live_issues: list[int] = []
        async with self._deps.sessionmaker() as session:
            for task in await runs_store.non_terminal_worker_tasks(session):
                state = transition(task.state, TaskEvent.ABORT)
                await runs_store.update_worker_task(
                    session, task.id, state=state, escalation_reason=note
                )
                run = await runs_store.get_run(session, task.run_id)
                if run is not None and not run.dry_run:
                    live_issues.append(task.issue_number)
            for refine in await runs_store.non_terminal_refine_tasks(session):
                await runs_store.update_refine_task(
                    session, refine.id, state=RefineState.ABORTED, error=note
                )
            for run in await runs_store.running_runs(session):
                await runs_store.finish_run(session, run.id, RunState.ABORTED, note)
            await session.commit()
        return sorted(set(live_issues))

    async def _comment_interrupted(self, issues: Sequence[int]) -> None:
        try:
            async with self._deps.sessionmaker() as session:
                settings = await settings_store.load_settings(session)
            gw = self._deps.gateway_factory(settings, audit_sink(self._deps.sessionmaker, None))
            for number in issues:
                await gw.comment(number, INTERRUPTED_COMMENT)
        except Exception:
            log.exception("could not comment on interrupted issues")

    async def _execute(self, run_id: int, settings: AppSettings, sink: AuditSink) -> None:
        try:
            state, note = await self._run(run_id, settings, sink)
        except Exception as exc:
            log.exception("run %d failed", run_id)
            state, note = RunState.FAILED, f"error: {type(exc).__name__}"
        async with self._deps.sessionmaker() as session:
            await runs_store.finish_run(session, run_id, state, note and note[:NOTE_CHARS])
            await session.commit()
        await sink(AuditKind.RUN_FINISHED, AuditOutcome.INFO, f"run {run_id} {state.value}", None)

    async def _run(self, run_id: int, settings: AppSettings, sink: AuditSink) -> Outcome:
        deps = self._deps
        started = deps.clock()
        gw = deps.gateway_factory(settings, sink)
        backends = make_backends(deps, settings)
        problems = await preflight_problems(deps, settings, gw, backends)
        if not problems:
            try:
                await apply_egress(deps, settings, backends)
            except Exception as exc:
                log.exception("egress allowlist could not be applied")
                problems = [f"egress allowlist could not be applied: {type(exc).__name__}"]
        if problems:
            await sink(
                AuditKind.PREFLIGHT_FAILED,
                AuditOutcome.BLOCKED,
                "preflight failed",
                {"problems": problems},
            )
            return RunState.PREFLIGHT_FAILED, "; ".join(problems)
        async with deps.sessionmaker() as session:
            rules = await load_rules(session)
            quotas = await remaining_quotas(session, settings, started)
        ctx = RunContext(
            deps=deps,
            settings=settings,
            run_id=run_id,
            gateway=gw,
            backends=backends,
            rules=rules,
            deadline=started + timedelta(hours=settings.schedule.window_hours),
            audit=sink,
        )
        candidates = await find_candidates(deps, settings, gw)
        async with asyncio.TaskGroup() as group:
            group.create_task(self._refines(ctx, candidates.refine, quotas.refines))
            workers = group.create_task(
                self._workers(
                    ctx, candidates.ready, quotas.issues, candidates.open_prs, quotas.fixes
                )
            )
        note = workers.result()
        return (RunState.COMPLETED if note is None else RunState.FAILED), note

    def _window_open(self, ctx: RunContext) -> bool:
        return self._deps.clock() < ctx.deadline

    async def _refines(self, ctx: RunContext, candidates: Sequence[Candidate], quota: int) -> None:
        for issue, comments in candidates[:quota]:
            if not self._window_open(ctx):
                return
            async with self._deps.sessionmaker() as session:
                task = await runs_store.add_refine_task(session, ctx.run_id, issue.number)
                await session.commit()
            await run_refine(ctx, task.id, issue, comments)

    async def _workers(
        self,
        ctx: RunContext,
        candidates: Sequence[Candidate],
        quota: int,
        open_prs: Sequence[OpenPr],
        fix_quota: int,
    ) -> str | None:
        try:
            selection = await select_issues(ctx, candidates, quota)
        except Exception as exc:
            log.exception("selection failed")
            return f"selection failed: {type(exc).__name__}: {exc}"
        by_number = {issue.number: (issue, comments) for issue, comments in candidates}
        for pick in selection.too_large:
            await self._escalate(
                ctx, pick.issue_number, f"the selector judged the issue too large: {pick.reason}"
            )
        for pick in selection.rejected:
            await self._escalate(
                ctx, pick.issue_number, f"the selector rejected the issue: {pick.reason}"
            )
        slots = asyncio.Semaphore(ctx.settings.limits.max_parallel_workers)
        budget = FixBudget(fix_quota)
        async with asyncio.TaskGroup() as group:
            for update in await self._pr_updates(ctx, open_prs):
                await slots.acquire()
                if not self._window_open(ctx) or budget.remaining <= 0:
                    slots.release()
                    break
                budget.remaining -= 1
                async with self._deps.sessionmaker() as session:
                    task = await runs_store.add_worker_task(
                        session,
                        ctx.run_id,
                        update.issue.number,
                        update.issue.title[:256],
                        TaskKind.PR_UPDATE,
                    )
                    await session.commit()
                group.create_task(self._update_slot(slots, ctx, task.id, update, budget))
            for pick in selection.picks:
                await slots.acquire()
                if not self._window_open(ctx):
                    slots.release()
                    break
                issue, comments = by_number[pick.issue_number]
                async with self._deps.sessionmaker() as session:
                    task = await runs_store.add_worker_task(
                        session, ctx.run_id, issue.number, issue.title[:256]
                    )
                    await session.commit()
                group.create_task(self._worker_slot(slots, ctx, task.id, issue, comments))
        return None

    async def _worker_slot(
        self,
        slots: asyncio.Semaphore,
        ctx: RunContext,
        task_id: int,
        issue: IssueRef,
        comments: Sequence[IssueComment],
    ) -> None:
        try:
            await run_worker(ctx, task_id, issue, comments)
        finally:
            slots.release()

    async def _pr_updates(self, ctx: RunContext, open_prs: Sequence[OpenPr]) -> list[PrUpdate]:
        updates: list[PrUpdate] = []
        for issue, comments, pull in open_prs:
            async with self._deps.sessionmaker() as session:
                last = await runs_store.last_pushed_task(session, issue.number)
            if last is None or last.head_sha != pull.head_sha:
                await ctx.audit(
                    AuditKind.PR_UPDATE_SKIPPED,
                    AuditOutcome.INFO,
                    f"PR #{pull.number} has commits Skeleton Crew did not push",
                    {"pr": pull.number, "issue": issue.number},
                )
                continue
            try:
                behind = await ctx.gateway.commits_behind(pull.head_sha)
            except Exception:
                log.exception("could not compare PR #%d with the base branch", pull.number)
                continue
            if behind > 0:
                updates.append(PrUpdate(issue, comments, pull, last.state is TaskState.DONE))
        return updates

    async def _update_slot(
        self,
        slots: asyncio.Semaphore,
        ctx: RunContext,
        task_id: int,
        update: PrUpdate,
        budget: FixBudget,
    ) -> None:
        try:
            if not await run_pr_update(ctx, task_id, update):
                budget.remaining += 1
        finally:
            slots.release()

    async def _escalate(self, ctx: RunContext, number: int, reason: str) -> None:
        try:
            await escalate_issue(ctx, number, reason)
        except Exception:
            log.exception("could not escalate #%d", number)
