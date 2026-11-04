import logging
from collections.abc import Sequence

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.branch_policy import pushed_by_controller
from skeleton_crew.domain.task_keys import worker_key
from skeleton_crew.domain.task_state import TaskEvent, TaskKind
from skeleton_crew.pipeline.branch_update import merge_base, resolve_conflicts
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.job_engine import Skip, run_job
from skeleton_crew.pipeline.run_setup import OpenPr
from skeleton_crew.pipeline.task_record import WorkerTaskRecord
from skeleton_crew.pipeline.worker_job import GitState, WorkerJob
from skeleton_crew.runtime.workspace import task_paths
from skeleton_crew.store import runs_store

MERGEABLE_POLLS = 3
MERGEABLE_POLL_SECONDS = 5.0

log = logging.getLogger(__name__)


async def choose_pr_updates(ctx: RunContext, open_prs: Sequence[OpenPr]) -> list[OpenPr]:
    owned: list[OpenPr] = []
    for pr in open_prs:
        async with ctx.deps.sessionmaker() as session:
            last = await runs_store.last_pushed_task(session, pr.issue.number)
        if pushed_by_controller(pr.pull.head_sha, last.head_sha if last else None):
            owned.append(pr)
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
    if mergeable is False:
        return True
    if mergeable:
        await _skipped(ctx, pr, f"PR #{number} merges cleanly")
    else:
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
        AuditKind.PR_UPDATE_SKIPPED,
        AuditOutcome.INFO,
        reason,
        {"pr": pr.pull.number, "issue": pr.issue.number},
    )


async def run_pr_fix(ctx: RunContext, task_id: int, pr: OpenPr) -> None:
    key = worker_key(task_id)
    job = WorkerJob(
        ctx=ctx,
        record=WorkerTaskRecord(ctx.deps.sessionmaker, task_id),
        issue=pr.issue,
        comments=pr.comments,
        key=key,
        paths=task_paths(ctx.deps.config.tasks_dir, key),
        branch=pr.pull.head_ref,
        kind=TaskKind.PR_FIX,
        pr_requested=True,
        git=GitState(remote_sha=pr.pull.head_sha),
    )

    async def work(job: WorkerJob) -> None:
        await job.record.fire(TaskEvent.STARTED, branch=job.branch, pr_number=pr.pull.number)
        merge = await merge_base(job)
        if not merge.conflicts:
            raise Skip("the PR merges cleanly after all")
        await resolve_conflicts(job, merge)

    await run_job(job, work)
