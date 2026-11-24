from skeleton_crew.domain.branch_policy import continuation_problem
from skeleton_crew.domain.task_state import JobMode, TaskEvent
from skeleton_crew.pipeline.branch_update import merge_base
from skeleton_crew.pipeline.candidates import Candidate
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.job_engine import iterate, run_job, update_branch
from skeleton_crew.pipeline.outcomes import Escalation
from skeleton_crew.pipeline.task_record import WorkerTaskRecord
from skeleton_crew.pipeline.texts import branch_taken_over, continued_comment
from skeleton_crew.pipeline.worker_job import WorkerJob
from skeleton_crew.pipeline.worker_session import ensure_worker_sandbox
from skeleton_crew.runtime.controller_git import tree_of
from skeleton_crew.store import runs_store


async def run_worker(
    ctx: RunContext, record: WorkerTaskRecord, candidate: Candidate, effort: str | None
) -> None:
    await run_job(WorkerJob.for_issue(ctx, record, candidate, effort), _work)


async def _work(job: WorkerJob) -> None:
    head = await job.ctx.gateway.branch_head(job.branch)
    await job.record.fire(TaskEvent.STARTED, branch=job.branch)
    if head is None:
        job.git.start_at(await job.ctx.gateway.clone(job.paths.controller_repo))
        await _code_from_base(job)
    else:
        await _check_takeover(job, head)
        await _continue_branch(job, head)


async def _check_takeover(job: WorkerJob, head: str) -> None:
    async with job.ctx.session() as session:
        last = await runs_store.last_pushed_task(session, job.issue.number)
    closed = await job.ctx.gateway.closed_unmerged_pr_for(job.branch)
    problem = continuation_problem(
        head, last.head_sha if last else None, closed.number if closed else None
    )
    if problem is not None:
        raise Escalation(branch_taken_over(job.branch, problem))


async def _continue_branch(job: WorkerJob, head: str) -> None:
    job.git.remote_sha = head
    merge = await merge_base(job)
    if merge.tree == await tree_of(job.paths.controller_repo, merge.base):
        job.git.start_at(merge.base)
        await _code_from_base(job)
        return
    job.progress.mode = JobMode.CONTINUED
    await job.ctx.gateway.comment(job.issue.number, continued_comment(job.branch))
    await update_branch(job, merge)


async def _code_from_base(job: WorkerJob) -> None:
    await ensure_worker_sandbox(job)
    await job.record.fire(TaskEvent.PREPARED, base_sha=job.git.base_sha)
    await iterate(job, None)
