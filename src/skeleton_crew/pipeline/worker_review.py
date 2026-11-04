import asyncio
import shutil

from skeleton_crew.agents.prompting import PromptName
from skeleton_crew.domain.schemas import ReviewResult
from skeleton_crew.domain.settings import Role
from skeleton_crew.pipeline.escalation import Escalation
from skeleton_crew.pipeline.sessions import (
    Sandbox,
    remove_sandbox,
    run_agent,
    start_sandbox,
    update_inbox,
)
from skeleton_crew.pipeline.texts import diff_stats
from skeleton_crew.pipeline.worker_job import WorkerJob, review_root
from skeleton_crew.pipeline.worker_policy import PolicyCheck
from skeleton_crew.runtime.workspace import binary_diff, make_readonly_copy, refresh_readonly_copy

CHANGES_FILE = "changes.diff"
SINCE_REVIEW_FILE = "since-review.diff"


def _prepare(job: WorkerJob, head_sha: str) -> None:
    root = review_root(job.paths)
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    make_readonly_copy(job.paths.controller_repo, root / "workspace", head_sha)


async def _start(job: WorkerJob, check: PolicyCheck, head_sha: str) -> Sandbox:
    root = review_root(job.paths)
    await remove_sandbox(job.ctx, job.review_sandbox)
    job.review_sandbox = None
    await asyncio.to_thread(_prepare, job, head_sha)
    job.review_sandbox = await start_sandbox(
        job.ctx,
        task_key=job.key,
        role=Role.REVIEWER,
        workspace=root / "workspace",
        writable=False,
        inbox=root / "inbox",
        transcripts=job.paths.transcripts,
        files={CHANGES_FILE: check.patch},
    )
    return job.review_sandbox


async def _refresh(job: WorkerJob, check: PolicyCheck, head_sha: str, reviewed_sha: str) -> None:
    root, repo = review_root(job.paths), job.paths.controller_repo
    since = await binary_diff(repo, reviewed_sha, head_sha)
    await asyncio.to_thread(refresh_readonly_copy, repo, root / "workspace", head_sha)
    files = {CHANGES_FILE: check.patch, SINCE_REVIEW_FILE: since}
    await asyncio.to_thread(update_inbox, root / "inbox", files)


async def review_change(
    job: WorkerJob, check: PolicyCheck, worker_summary: str, ci_state: str
) -> ReviewResult:
    head_sha = check.head_sha
    if head_sha is None:
        raise RuntimeError("review needs a commit")
    context: dict[str, object] = {
        "worker_summary": worker_summary,
        "diff_stats": diff_stats(check.changes),
        "contested": check.contested,
        "ci_state": ci_state,
    }
    sandbox, resume_id = job.review_sandbox, job.review_session_id
    if sandbox is not None and resume_id is not None and job.reviewed_sha is not None:
        await _refresh(job, check, head_sha, job.reviewed_sha)
        prompt = PromptName.REVIEWER_FOLLOWUP
    else:
        sandbox, resume_id = await _start(job, check, head_sha), None
        prompt = PromptName.REVIEWER
        context |= {"issue": job.issue, "comments": job.comments}
    reply = await run_agent(
        job.ctx,
        sandbox,
        role=Role.REVIEWER,
        prompt=prompt,
        context=context,
        output=ReviewResult,
        resume_session_id=resume_id,
    )
    await job.record.add_cost(reply.outcome.cost_usd)
    if reply.result is None:
        raise Escalation(f"review session failed: {reply.error}")
    job.review_session_id = reply.outcome.session_id
    job.reviewed_sha = head_sha
    return reply.result
