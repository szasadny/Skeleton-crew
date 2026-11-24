import asyncio
import shutil

from skeleton_crew.agents.prompting import PromptName
from skeleton_crew.domain.ci_policy import ReviewCheck
from skeleton_crew.domain.job_policy import CiResult, ReviewStep, review_step
from skeleton_crew.domain.schemas import ReviewResult
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import FollowUpKind, TaskEvent
from skeleton_crew.pipeline.outcomes import Escalation
from skeleton_crew.pipeline.sandbox import (
    Sandbox,
    remove_sandbox,
    review_paths,
    start_sandbox,
    update_inbox,
)
from skeleton_crew.pipeline.sessions import AgentCall, run_agent, settle
from skeleton_crew.pipeline.texts import (
    diff_stats,
    review_check_summary,
    review_text,
    review_too_large,
    tag_blockers,
)
from skeleton_crew.pipeline.worker_job import FollowUp, WorkerJob, WorkerReport, next_round
from skeleton_crew.pipeline.worker_policy import PassedCheck
from skeleton_crew.runtime.controller_git import (
    binary_diff,
    make_readonly_copy,
    refresh_readonly_copy,
)

CHANGES_FILE = "changes.diff"
SINCE_REVIEW_FILE = "since-review.diff"


def _prepare(job: WorkerJob, head_sha: str) -> None:
    review = review_paths(job.paths)
    shutil.rmtree(review.root, ignore_errors=True)
    review.root.mkdir(parents=True)
    make_readonly_copy(job.paths.controller_repo, review.agent_repo, head_sha)


async def _start(job: WorkerJob, check: PassedCheck) -> Sandbox:
    reviewer = job.reviewer
    await remove_sandbox(job.ctx, reviewer.sandbox)
    reviewer.sandbox = None
    await asyncio.to_thread(_prepare, job, check.head_sha)
    files = {CHANGES_FILE: check.patch}
    paths = review_paths(job.paths)
    reviewer.sandbox = await start_sandbox(job.ctx, job.key, Role.REVIEWER, paths, files)
    return reviewer.sandbox


async def _refresh(job: WorkerJob, check: PassedCheck, reviewed_sha: str) -> None:
    review, repo = review_paths(job.paths), job.paths.controller_repo
    since = await binary_diff(repo, reviewed_sha, check.head_sha)
    await asyncio.to_thread(refresh_readonly_copy, repo, review.agent_repo, check.head_sha)
    files = {CHANGES_FILE: check.patch, SINCE_REVIEW_FILE: since}
    await asyncio.to_thread(update_inbox, review.inbox, files)


async def review_change(
    job: WorkerJob, check: PassedCheck, worker_summary: str, ci: CiResult
) -> ReviewResult:
    context: dict[str, object] = {
        "worker_summary": worker_summary,
        "diff_stats": diff_stats(check.changes),
        "contested": check.contested,
        "ci_state": ci.value,
    }
    reviewer = job.reviewer
    sandbox, resume_id = reviewer.sandbox, reviewer.session_id
    if sandbox is not None and resume_id is not None and reviewer.reviewed_sha is not None:
        await _refresh(job, check, reviewer.reviewed_sha)
        prompt = PromptName.REVIEWER_FOLLOWUP
    else:
        sandbox, resume_id = await _start(job, check), None
        prompt = PromptName.REVIEWER
        context |= {"issue": job.issue, "comments": job.comments}
    reply = await run_agent(
        job.ctx, sandbox, AgentCall(prompt, context, ReviewResult, resume=resume_id)
    )
    result = await settle(job.ctx, job.record, reply, "review")
    await job.record.add_blockers(tag_blockers(Role.REVIEWER, result.blockers))
    reviewer.session_id = reply.outcome.session_id
    reviewer.reviewed_sha = check.head_sha
    return result


async def review(
    job: WorkerJob, check: PassedCheck, report: WorkerReport, ci: CiResult
) -> FollowUp | None:
    gw, max_chars = job.ctx.gateway, job.ctx.settings.limits.max_comment_chars
    result = await review_change(job, check, report.summary, ci)
    head = job.git.tip_sha
    summary = review_check_summary(result, report.blockers, max_chars)
    step = review_step(result.verdict)
    if step is ReviewStep.APPROVED:
        await gw.set_review_check(sha=head, state=ReviewCheck.PASSED, summary=summary)
        await job.record.fire(TaskEvent.REVIEW_APPROVED)
        return None
    await gw.set_review_check(sha=head, state=ReviewCheck.CHANGES_REQUESTED, summary=summary)
    if step is ReviewStep.TOO_LARGE:
        raise Escalation(review_too_large(result.summary))
    await next_round(job, FollowUpKind.REVIEW_CHANGES)
    return FollowUp(FollowUpKind.REVIEW_CHANGES, review_text(result))
