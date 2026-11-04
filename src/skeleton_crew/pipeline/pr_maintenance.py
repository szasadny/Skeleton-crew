import logging
from collections.abc import Sequence
from dataclasses import dataclass

from skeleton_crew.domain.task_state import TaskEvent, TaskKind
from skeleton_crew.domain.trust_policy import IssueComment
from skeleton_crew.github.gateway import IssueRef, PullRef
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.task_record import WorkerTaskRecord
from skeleton_crew.pipeline.worker_flow import run_job, update_branch, worker_key
from skeleton_crew.pipeline.worker_job import WorkerJob
from skeleton_crew.runtime.workspace import task_paths

log = logging.getLogger(__name__)


@dataclass
class FixBudget:
    remaining: int


@dataclass(frozen=True)
class PrUpdate:
    issue: IssueRef
    comments: Sequence[IssueComment]
    pull: PullRef
    reviewed: bool


async def run_pr_update(ctx: RunContext, task_id: int, update: PrUpdate) -> bool:
    key = worker_key(task_id)
    job = WorkerJob(
        ctx=ctx,
        record=WorkerTaskRecord(ctx.deps.sessionmaker, task_id),
        issue=update.issue,
        comments=update.comments,
        key=key,
        paths=task_paths(ctx.deps.config.tasks_dir, key),
        branch=update.pull.head_ref,
        kind=TaskKind.PR_UPDATE,
        remote_sha=update.pull.head_sha,
        pr_requested=True,
    )

    async def work(job: WorkerJob) -> None:
        await job.record.fire(TaskEvent.PREPARED, branch=job.branch, pr_number=update.pull.number)
        await update_branch(job, update.reviewed)

    await run_job(job, work)
    return job.kind is TaskKind.PR_FIX
