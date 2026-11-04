from dataclasses import dataclass

from skeleton_crew.domain.task_state import TaskEvent
from skeleton_crew.pipeline.escalation import Escalation
from skeleton_crew.pipeline.job_engine import Skip, deliver, iterate
from skeleton_crew.pipeline.texts import COMMIT_AUTHOR, merge_message
from skeleton_crew.pipeline.worker_job import WorkerJob
from skeleton_crew.pipeline.worker_policy import inspect_commit
from skeleton_crew.runtime.workspace import commit_tree, merge_trees, tree_of

CONFLICTS_MESSAGE = "Unresolved merge (local only)"


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
        raise Skip("the branch changed while the update was prepared")
    job.git.base_sha = base
    attempt = await merge_trees(repo, head, base)
    return BaseMerge(base, head, attempt.tree, attempt.conflicts)


async def resolve_conflicts(job: WorkerJob, merge: BaseMerge) -> None:
    conflicted = await commit_tree(
        job.paths.controller_repo,
        tree=merge.tree,
        parents=[merge.head],
        message=CONFLICTS_MESSAGE,
        author=COMMIT_AUTHOR,
    )
    job.git.resolve_merge(conflicted, merge.head, merge.conflicts)
    await job.record.fire(TaskEvent.PREPARED, base_sha=merge.base)
    await iterate(job, None)


async def publish_clean_merge(job: WorkerJob, merge: BaseMerge) -> None:
    repo = job.paths.controller_repo
    if merge.tree == await tree_of(repo, merge.head):
        merged = merge.head
    else:
        merged = await commit_tree(
            repo,
            tree=merge.tree,
            parents=[merge.head, merge.base],
            message=merge_message(job.ctx.settings.repo.base_branch, job.branch),
            author=COMMIT_AUTHOR,
        )
    job.git.continue_from(merged)
    await job.record.fire(TaskEvent.PREPARED, base_sha=merge.base)
    await job.record.fire(TaskEvent.CODE_DONE)
    check = await inspect_commit(job, merged, b"")
    if check.violations:
        raise Escalation(f"merging the base branch breaks policy: {'; '.join(check.violations)}")
    follow_up = await deliver(job, check, merged, None)
    if follow_up is not None:
        await iterate(job, follow_up)
