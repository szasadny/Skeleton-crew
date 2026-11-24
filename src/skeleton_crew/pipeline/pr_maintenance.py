import logging
from collections.abc import Sequence
from dataclasses import dataclass

from skeleton_crew.core.audit import AuditEntry, AuditKind, AuditOutcome
from skeleton_crew.domain.branch_policy import (
    Mergeability,
    mergeability,
    needs_finishing,
    pushed_by_controller,
)
from skeleton_crew.domain.task_state import TaskEvent
from skeleton_crew.pipeline.branch_update import merge_base
from skeleton_crew.pipeline.candidates import OpenPr
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.job_engine import run_job, update_branch
from skeleton_crew.pipeline.outcomes import Skip
from skeleton_crew.pipeline.task_record import WorkerTaskRecord
from skeleton_crew.pipeline.worker_job import WorkerJob
from skeleton_crew.store import runs_store

MERGEABLE_POLLS = 3
MERGEABLE_POLL_SECONDS = 5.0
CLEAN_AFTER_ALL = "the PR merges cleanly after all"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrFix:
    pr: OpenPr
    unfinished: bool


async def choose_pr_fixes(ctx: RunContext, open_prs: Sequence[OpenPr]) -> list[PrFix]:
    owned: list[PrFix] = []
    for pr in open_prs:
        async with ctx.session() as session:
            last = await runs_store.last_pushed_task(session, pr.issue.number)
        if last is not None and pushed_by_controller(pr.pull.head_sha, last.head_sha):
            owned.append(PrFix(pr, unfinished=needs_finishing(last.state)))
        else:
            await _skipped(ctx, pr, f"PR #{pr.pull.number} has commits Skeleton Crew did not push")
    return owned


async def has_conflicts(ctx: RunContext, pr: OpenPr) -> bool:
    number = pr.pull.number
    try:
        mergeable = await _poll_mergeable(ctx, number)
    except Exception:
        log.exception("could not read whether PR #%d merges cleanly", number)
        return False
    match mergeability(mergeable):
        case Mergeability.CONFLICTS:
            return True
        case Mergeability.CLEAN:
            await _skipped(ctx, pr, f"PR #{number} merges cleanly")
        case Mergeability.UNKNOWN:
            await _skipped(ctx, pr, f"PR #{number}: mergeability unknown")
    return False


async def _poll_mergeable(ctx: RunContext, number: int) -> bool | None:
    for attempt in range(MERGEABLE_POLLS):
        if attempt:
            await ctx.deps.sleep(MERGEABLE_POLL_SECONDS)
        mergeable = await ctx.gateway.pr_mergeable(number)
        if mergeable is not None:
            return mergeable
    return None


async def _skipped(ctx: RunContext, pr: OpenPr, reason: str) -> None:
    await ctx.audit(
        AuditEntry(
            AuditKind.PR_FIX_SKIPPED,
            AuditOutcome.INFO,
            reason,
            {"pr": pr.pull.number, "issue": pr.issue.number},
        )
    )


async def run_pr_fix(ctx: RunContext, record: WorkerTaskRecord, fix: PrFix) -> None:
    async def work(job: WorkerJob) -> None:
        await job.record.fire(TaskEvent.STARTED, branch=job.branch, pr_number=fix.pr.pull.number)
        merge = await merge_base(job)
        if not merge.conflicts and not fix.unfinished:
            raise Skip(CLEAN_AFTER_ALL)
        await update_branch(job, merge)

    await run_job(WorkerJob.for_pr(ctx, record, fix.pr), work)
