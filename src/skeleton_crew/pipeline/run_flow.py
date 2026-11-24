import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import timedelta
from functools import partial

from skeleton_crew.core.audit import AuditEntry, AuditKind, AuditOutcome, AuditSink
from skeleton_crew.domain.settings import AppSettings
from skeleton_crew.domain.task_state import RunState, RunStep, TaskKind, cap_note
from skeleton_crew.github.gateway import Gateway, IssueRef
from skeleton_crew.pipeline.candidates import Candidate, Candidates, OpenPr, find_candidates
from skeleton_crew.pipeline.classify import screen_candidates
from skeleton_crew.pipeline.deps import PipelineDeps, RunContext
from skeleton_crew.pipeline.escalation import escalate_issue
from skeleton_crew.pipeline.outcomes import error_detail, error_note
from skeleton_crew.pipeline.pr_maintenance import choose_pr_fixes, has_conflicts, run_pr_fix
from skeleton_crew.pipeline.refine_flow import run_refine
from skeleton_crew.pipeline.retention import prune_task_dirs
from skeleton_crew.pipeline.run_setup import (
    Preflight,
    Quotas,
    RepoDocs,
    apply_egress,
    collect_repo_docs,
    load_required_checks,
    load_rules,
    preflight,
    remaining_quotas,
)
from skeleton_crew.pipeline.selector import select_issues
from skeleton_crew.pipeline.task_record import RefineTaskRecord, WorkerTaskRecord
from skeleton_crew.pipeline.texts import selection_escalations
from skeleton_crew.pipeline.worker_flow import run_worker
from skeleton_crew.store import runs_store

USAGE_LIMIT_NOTE = "agent usage limit reached"

log = logging.getLogger(__name__)

type InStep = Callable[[RunStep], AbstractContextManager[None]]
type SlotWork = Callable[[WorkerTaskRecord], Awaitable[None]]


@dataclass(frozen=True)
class RunStart:
    run_id: int
    settings: AppSettings
    audit: AuditSink


@dataclass(frozen=True)
class RunEnd:
    state: RunState
    note: str | None = None


@dataclass(frozen=True)
class Selection:
    picks: list[Candidate]
    note: str | None = None


@dataclass(frozen=True)
class _Setup:
    checked: Preflight
    required: frozenset[str]
    docs: RepoDocs


class _SetupFailed(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


async def execute_run(deps: PipelineDeps, start: RunStart, in_step: InStep) -> None:
    try:
        end = await _run(deps, start, in_step)
    except Exception as exc:
        log.exception("run %d failed", start.run_id)
        end = RunEnd(RunState.FAILED, error_note(exc))
    note = None if end.note is None else cap_note(end.note)
    async with deps.sessionmaker() as session:
        await runs_store.finish_run(session, start.run_id, end.state, note)
        await session.commit()
    finished = f"run {start.run_id} {end.state.value}"
    await start.audit(AuditEntry(AuditKind.RUN_FINISHED, AuditOutcome.INFO, finished))


async def _run(deps: PipelineDeps, start: RunStart, in_step: InStep) -> RunEnd:
    settings, started = start.settings, deps.clock()
    with in_step(RunStep.PREFLIGHT):
        gw = deps.gateway_factory(settings, start.audit)
        try:
            setup = await _setup(deps, start, gw)
        except _SetupFailed as failed:
            return await _preflight_failed(start, failed.problems)
        await prune_task_dirs(deps, started, settings.limits.task_retention_days)
        async with deps.sessionmaker() as session:
            rules = await load_rules(session)
            quotas = await remaining_quotas(session, settings, started)
        ctx = RunContext(
            deps=deps,
            settings=settings,
            run_id=start.run_id,
            gateway=gw,
            backends=setup.checked.backends,
            rules=rules,
            repo_docs=setup.docs.by_backend,
            deadline=started + timedelta(hours=settings.schedule.window_hours),
            audit=start.audit,
            required_checks=setup.required,
            classifier=setup.checked.classifier,
            docs_clone=setup.docs.clone,
        )
    with in_step(RunStep.CANDIDATES):
        screened = await screen_candidates(
            ctx, await find_candidates(deps, settings, gw, ctx.audit)
        )
        for number, reason in screened.escalations.items():
            await _escalate(ctx, number, reason)
    candidates = screened.candidates
    async with asyncio.TaskGroup() as group:
        group.create_task(_refines(ctx, candidates.refine, quotas.refines))
        workers = group.create_task(_workers(ctx, candidates, quotas, screened.efforts, in_step))
    return _run_end(ctx, workers.result())


def _run_end(ctx: RunContext, note: str | None) -> RunEnd:
    if ctx.halted.is_set():
        note = USAGE_LIMIT_NOTE if note is None else f"{USAGE_LIMIT_NOTE}; {note}"
    return RunEnd(RunState.COMPLETED if note is None else RunState.FAILED, note)


async def _setup(deps: PipelineDeps, start: RunStart, gw: Gateway) -> _Setup:
    settings = start.settings
    checked = await preflight(deps, settings, gw)
    if checked.problems:
        raise _SetupFailed(checked.problems)
    required = await _guarded_step(
        "required checks could not be read",
        load_required_checks(gw, settings.repo.base_branch),
    )
    await _guarded_step(
        "egress allowlist could not be applied",
        apply_egress(deps, settings, checked.backends),
    )
    docs = await _guarded_step(
        "repository docs could not be read",
        collect_repo_docs(deps, gw, start.run_id, checked.backends),
    )
    return _Setup(checked, required, docs)


async def _guarded_step[T](label: str, step: Awaitable[T]) -> T:
    try:
        return await step
    except Exception as exc:
        log.exception("%s", label)
        raise _SetupFailed([f"{label}: {type(exc).__name__}"]) from exc


async def _preflight_failed(start: RunStart, problems: list[str]) -> RunEnd:
    await start.audit(
        AuditEntry(
            AuditKind.PREFLIGHT_FAILED,
            AuditOutcome.BLOCKED,
            "preflight failed",
            {"problems": problems},
        )
    )
    return RunEnd(RunState.PREFLIGHT_FAILED, "; ".join(problems))


async def _escalate(ctx: RunContext, number: int, reason: str) -> None:
    try:
        await escalate_issue(ctx, number, reason)
    except Exception:
        log.exception("could not escalate #%d", number)


async def _refines(ctx: RunContext, candidates: Sequence[Candidate], quota: int) -> None:
    for candidate in candidates[:quota]:
        if not ctx.may_start():
            return
        number = candidate.issue.number
        record = await RefineTaskRecord.create(ctx.deps.sessionmaker, ctx.run_id, number)
        await run_refine(ctx, record, candidate)


async def _workers(
    ctx: RunContext,
    candidates: Candidates,
    quotas: Quotas,
    efforts: Mapping[int, str],
    in_step: InStep,
) -> str | None:
    selection = await _select(ctx, candidates.ready, quotas.issues, in_step)
    async with asyncio.TaskGroup() as group:
        slots = _Slots(group, ctx.settings.limits.max_parallel_workers)
        await _start_pr_fixes(ctx, slots, candidates.open_prs, quotas.fixes)
        await _start_issues(ctx, slots, selection.picks, efforts)
    return selection.note


async def _select(
    ctx: RunContext, candidates: Sequence[Candidate], quota: int, in_step: InStep
) -> Selection:
    try:
        with in_step(RunStep.SELECTOR):
            selection = await select_issues(ctx, candidates, quota)
    except Exception as exc:
        log.exception("selection failed")
        return Selection([], f"selection failed: {error_detail(exc)}")
    for number, reason in selection_escalations(selection):
        await _escalate(ctx, number, reason)
    by_number = {c.issue.number: c for c in candidates}
    return Selection([by_number[pick.issue_number] for pick in selection.picks])


class _Slots:
    def __init__(self, group: asyncio.TaskGroup, size: int) -> None:
        self._group = group
        self._free = asyncio.Semaphore(size)

    async def launch(
        self, ctx: RunContext, issue: IssueRef, kind: TaskKind, work: SlotWork
    ) -> bool:
        await self._free.acquire()
        if not ctx.may_start():
            self._free.release()
            return False
        record = await WorkerTaskRecord.create(ctx.deps.sessionmaker, ctx.run_id, issue, kind)
        self._group.create_task(_in_slot(self._free, partial(work, record)))
        return True


async def _in_slot(slots: asyncio.Semaphore, work: Callable[[], Awaitable[None]]) -> None:
    try:
        await work()
    finally:
        slots.release()


async def _start_pr_fixes(
    ctx: RunContext, slots: _Slots, open_prs: Sequence[OpenPr], quota: int
) -> None:
    remaining = quota
    for fix in await choose_pr_fixes(ctx, open_prs):
        if remaining <= 0 or not ctx.may_start():
            return
        if not fix.unfinished and not await has_conflicts(ctx, fix.pr):
            continue
        work = partial(run_pr_fix, ctx, fix=fix)
        if not await slots.launch(ctx, fix.pr.issue, TaskKind.PR_FIX, work):
            return
        remaining -= 1


async def _start_issues(
    ctx: RunContext, slots: _Slots, picks: Sequence[Candidate], efforts: Mapping[int, str]
) -> None:
    for pick in picks:
        work = partial(run_worker, ctx, candidate=pick, effort=efforts.get(pick.issue.number))
        if not await slots.launch(ctx, pick.issue, TaskKind.ISSUE, work):
            return
