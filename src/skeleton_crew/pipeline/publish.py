from collections.abc import Sequence
from dataclasses import dataclass

from skeleton_crew.domain.ci_policy import FailedJob, ReviewCheck
from skeleton_crew.domain.job_policy import UNTRUSTED_OUTPUT_CHARS, CiResult, ci_result
from skeleton_crew.domain.task_state import FollowUpKind
from skeleton_crew.pipeline.ci_watcher import CiWait, wait_for_ci
from skeleton_crew.pipeline.outcomes import Escalation
from skeleton_crew.pipeline.texts import CI_ESCALATIONS, REVIEW_PENDING_SUMMARY, pr_body, pr_title
from skeleton_crew.pipeline.worker_job import FollowUp, WorkerJob, WorkerReport, next_round


@dataclass(frozen=True)
class CiOutcome:
    result: CiResult
    follow_up: FollowUp | None = None


async def publish(job: WorkerJob, head_sha: str, report: WorkerReport) -> None:
    gw, max_chars = job.ctx.gateway, job.ctx.settings.limits.max_comment_chars
    await gw.push_branch(job.paths.controller_repo, job.branch, head_sha, job.git.remote_sha)
    job.git.remote_sha = head_sha
    # Recorded only after the push: PR maintenance trusts this as the head the controller owns.
    await job.record.update(head_sha=head_sha)
    await gw.set_review_check(
        sha=head_sha, state=ReviewCheck.PENDING, summary=REVIEW_PENDING_SUMMARY
    )
    if job.progress.pr_requested:
        return
    job.progress.pr_requested = True
    pull = await gw.open_pr(
        branch=job.branch,
        title=pr_title(report.title),
        body=pr_body(job.issue.number, report.summary, report.blockers, max_chars),
    )
    if pull is not None:
        await job.record.update(pr_number=pull.number)


async def await_ci(job: WorkerJob, head_sha: str) -> CiOutcome:
    ctx = job.ctx
    if ctx.gateway.dry_run:
        return CiOutcome(CiResult.DRY_RUN)
    wait = CiWait.from_limits(ctx.settings.limits, ctx.required_checks)
    status = await wait_for_ci(ctx.gateway, head_sha, wait, ctx.deps)
    result = ci_result(status.state, ctx.settings.repo.require_ci)
    escalation = CI_ESCALATIONS.get(result)
    if escalation is not None:
        raise Escalation(escalation)
    if result is not CiResult.FAILURE:
        return CiOutcome(result)
    await next_round(job, FollowUpKind.CI_FAILED)
    details = await _ci_details(job, status.failed_jobs)
    return CiOutcome(result, FollowUp(FollowUpKind.CI_FAILED, details))


async def _ci_details(job: WorkerJob, failed_jobs: Sequence[FailedJob]) -> str:
    names = "\n".join(f"- {j.name}" for j in failed_jobs) or "- (unknown job)"
    ids = [j.job_id for j in failed_jobs if j.job_id is not None]
    logs = await job.ctx.gateway.failed_job_logs(ids, UNTRUSTED_OUTPUT_CHARS) if ids else ""
    return f"Failed jobs:\n{names}\n\n{logs}".strip()
