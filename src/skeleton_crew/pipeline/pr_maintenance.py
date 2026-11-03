import logging
from collections.abc import Sequence
from dataclasses import dataclass

from skeleton_crew.domain.task_state import TaskEvent, TaskKind
from skeleton_crew.domain.trust_policy import IssueComment
from skeleton_crew.github.gateway import IssueRef, PullRef
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.escalation import Escalation
from skeleton_crew.pipeline.task_record import WorkerTaskRecord
from skeleton_crew.pipeline.texts import CARRIED_REVIEW, COMMIT_AUTHOR, MERGE_SUMMARY, merge_message
from skeleton_crew.pipeline.worker_flow import (
    Skip,
    await_ci,
    iterate,
    publish,
    review,
    run_job,
    worker_key,
)
from skeleton_crew.pipeline.worker_job import WorkerJob
from skeleton_crew.pipeline.worker_policy import inspect_commit
from skeleton_crew.runtime.workspace import commit_tree, merge_trees, same_change, task_paths

CONFLICTS_MESSAGE = "Unresolved merge (local only)"

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
        await _maintain(job, update.reviewed)

    await run_job(job, work)
    return job.kind is TaskKind.PR_FIX


async def _maintain(job: WorkerJob, reviewed: bool) -> None:
    ctx, repo = job.ctx, job.paths.controller_repo
    base = await ctx.gateway.clone(repo)
    head = await ctx.gateway.fetch_branch(repo, job.branch)
    if head != job.remote_sha:
        raise Skip("the PR branch changed while the update was prepared")
    job.base_sha = base
    attempt = await merge_trees(repo, head, base)
    if attempt.conflicts:
        job.patch_base_sha = await commit_tree(
            repo, tree=attempt.tree, parents=[head], message=CONFLICTS_MESSAGE, author=COMMIT_AUTHOR
        )
        job.tip_sha, job.merge_parents, job.conflicts = head, (head, base), attempt.conflicts
        await job.record.fire(TaskEvent.PREPARED, base_sha=base)
        await iterate(job, None)
        return
    merged = await commit_tree(
        repo,
        tree=attempt.tree,
        parents=[head, base],
        message=merge_message(ctx.settings.repo.base_branch, job.branch),
        author=COMMIT_AUTHOR,
    )
    job.patch_base_sha = job.tip_sha = merged
    await job.record.fire(TaskEvent.PREPARED, base_sha=base)
    await job.record.fire(TaskEvent.CODE_DONE)
    check = await inspect_commit(job, merged, b"")
    if check.violations:
        raise Escalation(f"merging the base branch breaks policy: {'; '.join(check.violations)}")
    await job.record.fire(TaskEvent.POLICY_PASSED)
    await publish(job, None, merged)
    ci_state, follow_up = await await_ci(job, merged)
    if follow_up is None:
        await job.record.fire(TaskEvent.CI_PASSED)
        if reviewed and await same_change(repo, old_head=head, base_sha=base, new_head=merged):
            await ctx.gateway.set_review_check(sha=merged, success=True, summary=CARRIED_REVIEW)
            await job.record.fire(TaskEvent.REVIEW_APPROVED)
            return
        follow_up = await review(job, check, MERGE_SUMMARY, ci_state)
        if follow_up is None:
            return
    await iterate(job, follow_up)
