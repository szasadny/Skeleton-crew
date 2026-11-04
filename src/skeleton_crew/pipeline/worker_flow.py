from collections.abc import Sequence

from skeleton_crew.domain.branch_policy import branch_name
from skeleton_crew.domain.task_keys import worker_key
from skeleton_crew.domain.task_state import TaskEvent
from skeleton_crew.domain.trust_policy import IssueComment
from skeleton_crew.github.gateway import IssueRef
from skeleton_crew.pipeline.branch_update import (
    merge_base,
    publish_clean_merge,
    resolve_conflicts,
)
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.job_engine import iterate, run_job, start_worker_sandbox
from skeleton_crew.pipeline.task_record import WorkerTaskRecord
from skeleton_crew.pipeline.texts import continued_comment
from skeleton_crew.pipeline.worker_job import WorkerJob
from skeleton_crew.runtime.workspace import task_paths, tree_of


async def run_worker(
    ctx: RunContext, task_id: int, issue: IssueRef, comments: Sequence[IssueComment]
) -> None:
    key = worker_key(task_id)
    job = WorkerJob(
        ctx=ctx,
        record=WorkerTaskRecord(ctx.deps.sessionmaker, task_id),
        issue=issue,
        comments=comments,
        key=key,
        paths=task_paths(ctx.deps.config.tasks_dir, key),
        branch=branch_name(issue.number, issue.title),
    )
    await run_job(job, _work)


async def _work(job: WorkerJob) -> None:
    head = await job.ctx.gateway.branch_head(job.branch)
    await job.record.fire(TaskEvent.STARTED, branch=job.branch)
    if head is None:
        job.git.start_at(await job.ctx.gateway.clone(job.paths.controller_repo))
        await _code_from_base(job)
    else:
        await _continue_branch(job, head)


async def _continue_branch(job: WorkerJob, head: str) -> None:
    job.git.remote_sha, job.continues_branch = head, True
    merge = await merge_base(job)
    if merge.tree == await tree_of(job.paths.controller_repo, merge.base):
        job.continues_branch = False
        job.git.continue_from(merge.base)
        await _code_from_base(job)
        return
    await job.ctx.gateway.comment(job.issue.number, continued_comment(job.branch))
    if merge.conflicts:
        await resolve_conflicts(job, merge)
    else:
        await publish_clean_merge(job, merge)


async def _code_from_base(job: WorkerJob) -> None:
    await start_worker_sandbox(job)
    await job.record.fire(TaskEvent.PREPARED, base_sha=job.git.base_sha)
    await iterate(job, None)
