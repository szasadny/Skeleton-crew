import logging
from collections.abc import Sequence
from dataclasses import dataclass

from skeleton_crew.core.audit import AuditKind, AuditOutcome
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

log = logging.getLogger(__name__)


@dataclass
class FixBudget:
    remaining: int

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True

    def refund(self) -> None:
        self.remaining += 1


async def choose_pr_updates(ctx: RunContext, open_prs: Sequence[OpenPr]) -> list[OpenPr]:
    updates: list[OpenPr] = []
    for pr in open_prs:
        pull = pr.pull
        async with ctx.deps.sessionmaker() as session:
            last = await runs_store.last_pushed_task(session, pr.issue.number)
        if last is None or last.head_sha != pull.head_sha:
            await ctx.audit(
                AuditKind.PR_UPDATE_SKIPPED,
                AuditOutcome.INFO,
                f"PR #{pull.number} has commits Skeleton Crew did not push",
                {"pr": pull.number, "issue": pr.issue.number},
            )
            continue
        try:
            behind = await ctx.gateway.commits_behind(pull.head_sha)
        except Exception:
            log.exception("could not compare PR #%d with the base branch", pull.number)
            continue
        if behind > 0:
            updates.append(pr)
    return updates


async def run_pr_update(ctx: RunContext, task_id: int, pr: OpenPr, budget: FixBudget) -> None:
    key = worker_key(task_id)
    job = WorkerJob(
        ctx=ctx,
        record=WorkerTaskRecord(ctx.deps.sessionmaker, task_id),
        issue=pr.issue,
        comments=pr.comments,
        key=key,
        paths=task_paths(ctx.deps.config.tasks_dir, key),
        branch=pr.pull.head_ref,
        kind=TaskKind.PR_UPDATE,
        pr_requested=True,
        git=GitState(remote_sha=pr.pull.head_sha),
    )

    async def work(job: WorkerJob) -> None:
        await job.record.fire(TaskEvent.STARTED, branch=job.branch, pr_number=pr.pull.number)
        merge = await merge_base(job)
        if not merge.conflicts:
            raise Skip("the PR merges cleanly")
        await _become_fix(job)
        await resolve_conflicts(job, merge)

    await run_job(job, work)
    if job.kind is TaskKind.PR_UPDATE:
        budget.refund()


async def _become_fix(job: WorkerJob) -> None:
    # Only a job that needs an agent counts against conflict_fixes_per_day.
    job.kind = TaskKind.PR_FIX
    await job.record.update(kind=job.kind)
