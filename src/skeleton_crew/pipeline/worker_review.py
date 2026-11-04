import asyncio
import shutil

from skeleton_crew.agents.prompting import PromptName
from skeleton_crew.domain.schemas import ReviewResult
from skeleton_crew.domain.settings import Role
from skeleton_crew.pipeline.escalation import Escalation
from skeleton_crew.pipeline.sessions import Sandbox, remove_sandbox, run_agent, start_sandbox
from skeleton_crew.pipeline.texts import diff_stats
from skeleton_crew.pipeline.worker_job import WorkerJob, review_root
from skeleton_crew.pipeline.worker_policy import PolicyCheck
from skeleton_crew.runtime.workspace import make_readonly_copy

CHANGES_FILE = "changes.diff"


def _prepare(job: WorkerJob, head_sha: str) -> None:
    root = review_root(job.paths)
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    make_readonly_copy(job.paths.controller_repo, root / "workspace", head_sha)


async def review_change(
    job: WorkerJob, check: PolicyCheck, worker_summary: str, ci_state: str
) -> ReviewResult:
    if check.head_sha is None:
        raise RuntimeError("review needs a commit")
    ctx = job.ctx
    root = review_root(job.paths)
    sandbox: Sandbox | None = None
    try:
        await asyncio.to_thread(_prepare, job, check.head_sha)
        sandbox = await start_sandbox(
            ctx,
            task_key=job.key,
            role=Role.REVIEWER,
            workspace=root / "workspace",
            writable=False,
            inbox=root / "inbox",
            transcripts=job.paths.transcripts,
            files={CHANGES_FILE: check.patch},
        )
        reply = await run_agent(
            ctx,
            sandbox,
            role=Role.REVIEWER,
            prompt=PromptName.REVIEWER,
            context={
                "issue": job.issue,
                "comments": job.comments,
                "worker_summary": worker_summary,
                "diff_stats": diff_stats(check.changes),
                "contested": check.contested,
                "ci_state": ci_state,
            },
            output=ReviewResult,
        )
    finally:
        await remove_sandbox(ctx, sandbox)
        await asyncio.to_thread(shutil.rmtree, root, ignore_errors=True)
    await job.record.add_cost(reply.outcome.cost_usd)
    if reply.result is None:
        raise Escalation(f"review session failed: {reply.error}")
    return reply.result
