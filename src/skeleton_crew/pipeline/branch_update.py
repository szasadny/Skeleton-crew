from dataclasses import dataclass

from skeleton_crew.domain.ci_policy import CiState
from skeleton_crew.domain.task_state import TaskEvent
from skeleton_crew.pipeline.outcomes import Escalation, Skip
from skeleton_crew.pipeline.texts import (
    CONFLICTS_MESSAGE,
    RERUN_CI_MESSAGE,
    merge_breaks_policy,
    merge_message,
)
from skeleton_crew.pipeline.worker_job import WorkerJob
from skeleton_crew.pipeline.worker_policy import PassedCheck, commit_spec, inspect_commit
from skeleton_crew.runtime.controller_git import commit_tree, merge_trees, tree_of

BRANCH_CHANGED = "the branch changed while the update was prepared"


@dataclass(frozen=True)
class BaseMerge:
    base: str
    head: str
    tree: str
    conflicts: tuple[str, ...]


async def merge_base(job: WorkerJob) -> BaseMerge:
    ctx, repo = job.ctx, job.paths.controller_repo
    base = await ctx.gateway.clone(repo)
    head = await ctx.gateway.fetch_branch(repo, job.branch)
    if head != job.git.remote_sha:
        raise Skip(BRANCH_CHANGED)
    attempt = await merge_trees(repo, head, base)
    return BaseMerge(base, head, attempt.tree, attempt.conflicts)


async def _commit(job: WorkerJob, tree: str, parents: tuple[str, ...], message: str) -> str:
    spec = commit_spec(parents, message)
    return await commit_tree(job.paths.controller_repo, tree=tree, commit=spec)


async def prepare_conflicts(job: WorkerJob, merge: BaseMerge) -> None:
    conflicted = await _commit(job, merge.tree, (merge.head,), CONFLICTS_MESSAGE)
    job.git.resolve_merge(conflicted, merge.head, merge.base, merge.conflicts)
    await job.record.fire(TaskEvent.PREPARED, base_sha=merge.base)


async def prepare_clean_merge(job: WorkerJob, merge: BaseMerge) -> PassedCheck:
    if merge.tree == await tree_of(job.paths.controller_repo, merge.head):
        merged = await _rerun_failed_ci(job, merge)
    else:
        message = merge_message(job.base_branch, job.branch)
        merged = await _commit(job, merge.tree, (merge.head, merge.base), message)
    job.git.merged(merge.base, merged)
    await job.record.fire(TaskEvent.PREPARED, base_sha=merge.base)
    await job.record.fire(TaskEvent.CODE_DONE)
    inspected = await inspect_commit(job, merged)
    check = inspected.passed()
    if check is None:
        raise Escalation(merge_breaks_policy(inspected.violations))
    return check


async def _rerun_failed_ci(job: WorkerJob, merge: BaseMerge) -> str:
    ctx = job.ctx
    status = await ctx.gateway.ci_status(merge.head, ctx.required_checks)
    if status.state is not CiState.FAILURE:
        return merge.head
    # An empty commit restarts CI without giving the App write access to Actions.
    return await _commit(job, merge.tree, (merge.head,), RERUN_CI_MESSAGE)
