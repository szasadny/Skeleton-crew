import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta

from skeleton_crew.agents.prompting import PromptName
from skeleton_crew.domain.contestable_checks import describe_checks
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.schemas import WorkerResult
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import TERMINAL_TASK_STATES, TaskEvent
from skeleton_crew.github.gateway import CiState, FailedJob
from skeleton_crew.pipeline.ci_watcher import wait_for_ci
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.escalation import Escalation, escalate_issue
from skeleton_crew.pipeline.repo_commands import run_repo_command
from skeleton_crew.pipeline.sessions import remove_sandbox, run_agent, start_sandbox
from skeleton_crew.pipeline.texts import (
    CONTINUED_SUMMARY,
    new_issue_text,
    pr_body,
    pr_title,
    review_check_summary,
    review_text,
)
from skeleton_crew.pipeline.worker_job import FollowUp, FollowUpKind, WorkerJob, remove_work_dirs
from skeleton_crew.pipeline.worker_policy import PolicyCheck, check_change
from skeleton_crew.pipeline.worker_review import review_change
from skeleton_crew.runtime.workspace import checkout_commit, make_agent_copy

CI_LOG_CHARS = 20_000
NOTE_CHARS = 1000
DRY_RUN_CI = "skipped (dry run, nothing was pushed)"

log = logging.getLogger(__name__)


class Skip(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


type Work = Callable[[WorkerJob], Awaitable[None]]


async def run_job(job: WorkerJob, work: Work) -> None:
    ctx, issue = job.ctx, job.issue
    outcome: Escalation | Skip | Exception | None = None
    try:
        async with asyncio.timeout(_seconds_left(ctx)):
            await work(job)
    except TimeoutError:
        outcome = Escalation("the worker timed out or the run window ended")
    except (Escalation, Skip) as exc:
        outcome = exc
    except Exception as exc:
        log.exception("worker for #%d failed", issue.number)
        outcome = exc
    finally:
        await remove_sandbox(ctx, job.sandbox)
        await remove_sandbox(ctx, job.reviewer.sandbox)
        await asyncio.to_thread(remove_work_dirs, job.paths)
    if outcome is not None:
        await _finish_unsuccessful(job, outcome)


def _seconds_left(ctx: RunContext) -> float:
    now = ctx.deps.clock()
    deadline = min(now + timedelta(hours=ctx.settings.limits.worker_timeout_hours), ctx.deadline)
    return max((deadline - now).total_seconds(), 0.0)


async def _finish_unsuccessful(job: WorkerJob, outcome: Escalation | Skip | Exception) -> None:
    if job.record.state in TERMINAL_TASK_STATES:
        return
    try:
        if isinstance(outcome, Skip):
            await job.record.fire(TaskEvent.ABORT, note=outcome.reason)
            return
        if isinstance(outcome, Escalation):
            note = outcome.reason[:NOTE_CHARS]
            blocked = await escalate_issue(job.ctx, job.issue.number, note)
            if blocked is None:
                await job.record.fire(TaskEvent.ESCALATE, note=note)
                return
            note = f"{note} ({blocked})"[:NOTE_CHARS]
        else:
            note = f"error: {type(outcome).__name__}"
        await job.record.fire(TaskEvent.ERROR, note=note)
    except Exception:
        log.exception("could not record the end of the worker for #%d", job.issue.number)


async def start_worker_sandbox(job: WorkerJob) -> None:
    ctx, paths = job.ctx, job.paths
    if job.on_existing_branch:
        await checkout_commit(paths.controller_repo, job.git.patch_base_sha)
    await asyncio.to_thread(make_agent_copy, paths)
    job.sandbox = await start_sandbox(
        ctx,
        task_key=job.key,
        role=Role.WORKER,
        workspace=paths.agent_repo,
        writable=True,
        inbox=paths.inbox,
        transcripts=paths.transcripts,
    )
    await job.record.update(container_id=job.sandbox.handle.id)
    command = ctx.settings.repo.setup_command
    if command:
        setup = await run_repo_command(job, command, "setup")
        if not setup.ok:
            raise Escalation(f"the setup command failed ({setup.status})")


async def iterate(job: WorkerJob, follow_up: FollowUp | None) -> None:
    while True:
        result = await _code(job, follow_up)
        await job.record.fire(TaskEvent.CODE_DONE)
        follow_up = await _run_checks(job)
        if follow_up is not None:
            continue
        check = await check_change(job, result)
        if check.violations or check.head_sha is None:
            follow_up = await _policy_failed(job, check)
            continue
        follow_up = await deliver(job, check, check.head_sha, result)
        if follow_up is None:
            return


async def deliver(
    job: WorkerJob, check: PolicyCheck, head_sha: str, result: WorkerResult | None
) -> FollowUp | None:
    job.git.committed(head_sha)
    await job.record.fire(TaskEvent.POLICY_PASSED)
    await publish(job, result, head_sha)
    ci_state, follow_up = await await_ci(job, head_sha)
    if follow_up is not None:
        return follow_up
    await job.record.fire(TaskEvent.CI_PASSED)
    summary = result.summary if result else CONTINUED_SUMMARY
    return await review(job, check, summary, ci_state)


async def _run_checks(job: WorkerJob) -> FollowUp | None:
    command = job.ctx.settings.repo.check_command
    # While resolving a merge only conflicted files may change; CI reports the rest.
    if not command or job.git.resolving_merge:
        return None
    checks = await run_repo_command(job, command, "check")
    if checks.ok:
        return None
    job.rounds.ci += 1
    limit = job.ctx.settings.limits.max_ci_rounds
    if job.rounds.ci > limit:
        raise Escalation(f"the check command still fails after {limit} fix rounds")
    await job.record.fire(TaskEvent.CHECK_FAILED, ci_rounds=job.rounds.ci)
    details = f"`{command}` failed ({checks.status}):\n\n{checks.output_tail}".strip()
    return FollowUp(FollowUpKind.CHECK_FAILED, details)


async def _policy_failed(job: WorkerJob, check: PolicyCheck) -> FollowUp:
    job.rounds.policy += 1
    _check_review_rounds(job)
    await job.record.fire(TaskEvent.POLICY_FAILED, policy_rounds=job.rounds.policy)
    return FollowUp(FollowUpKind.POLICY_FAILED, "\n".join(f"- {v}" for v in check.violations))


def _follow_up_context(follow_up: FollowUp) -> dict[str, object]:
    return {"kind": follow_up.kind.value, "details": follow_up.details}


def _prompt(job: WorkerJob, follow_up: FollowUp | None) -> tuple[PromptName, dict[str, object]]:
    if follow_up is not None and job.session_id is not None:
        return PromptName.WORKER_FOLLOWUP, _follow_up_context(follow_up)
    if job.on_existing_branch and job.session_id is None:
        update: dict[str, object] = {
            "conflicts": list(job.git.conflicts),
            "kind": None,
            "details": None,
        }
        if follow_up is not None:
            update |= _follow_up_context(follow_up)
        base = job.ctx.settings.repo.base_branch
        return PromptName.WORKER_UPDATE, _initial_context(job) | update | {"base_branch": base}
    if follow_up is None:
        return PromptName.WORKER, _initial_context(job)
    raise Escalation("the worker session cannot be resumed")


async def _code(job: WorkerJob, follow_up: FollowUp | None) -> WorkerResult:
    prompt, context = _prompt(job, follow_up)
    if job.sandbox is None:
        await start_worker_sandbox(job)
    if job.sandbox is None:
        raise RuntimeError("worker container is not running")
    reply = await run_agent(
        job.ctx,
        job.sandbox,
        role=Role.WORKER,
        prompt=prompt,
        context=context,
        output=WorkerResult,
        resume_session_id=job.session_id if prompt is PromptName.WORKER_FOLLOWUP else None,
    )
    if reply.outcome.session_id is not None:
        job.session_id = reply.outcome.session_id
    await job.record.add_cost(reply.outcome.cost_usd, session_id=job.session_id)
    if reply.result is None:
        raise Escalation(f"worker session failed: {reply.error}")
    await _report_new_issues(job, reply.result)
    job.objections |= {(o.check, o.path, o.line): o.reason for o in reply.result.objections}
    if reply.result.status != "done":
        raise Escalation(f"worker reported {reply.result.status}: {reply.result.note}")
    return reply.result


def _initial_context(job: WorkerJob) -> dict[str, object]:
    settings = job.ctx.settings
    return {
        "issue": job.issue,
        "comments": job.comments,
        "branch": job.branch,
        "setup_command": settings.repo.setup_command,
        "check_command": settings.repo.check_command,
        "contestable_checks": describe_checks(settings),
        "protected_paths": list(settings.repo.protected_paths),
        "max_new_issues": settings.limits.max_new_issues_per_worker,
    }


async def _report_new_issues(job: WorkerJob, result: WorkerResult) -> None:
    limits = job.ctx.settings.limits
    for new_issue in result.new_issues:
        if job.rounds.new_issues >= limits.max_new_issues_per_worker:
            log.info("new-issue cap reached for #%d", job.issue.number)
            return
        job.rounds.new_issues += 1
        title, body = new_issue_text(new_issue, job.issue.number, limits.max_comment_chars)
        try:
            await job.ctx.gateway.create_hitl_issue(title, body)
        except PolicyViolation as exc:
            log.warning("new issue from #%d was blocked: %s", job.issue.number, exc)


def _check_review_rounds(job: WorkerJob) -> None:
    allowed = job.ctx.settings.limits.max_review_rounds
    if job.rounds.policy + job.rounds.review > allowed:
        raise Escalation(f"no acceptable change after {allowed} policy or review rounds")


async def publish(job: WorkerJob, result: WorkerResult | None, head_sha: str) -> None:
    gw, max_chars = job.ctx.gateway, job.ctx.settings.limits.max_comment_chars
    await gw.push_branch(job.paths.controller_repo, job.branch, head_sha, job.git.remote_sha)
    job.git.remote_sha = head_sha
    # Recorded only after the push: PR maintenance trusts this as the head the controller owns.
    await job.record.update(head_sha=head_sha)
    if job.pr_requested:
        return
    job.pr_requested = True
    title, summary = (
        (result.pr_title, result.summary) if result else (job.issue.title, CONTINUED_SUMMARY)
    )
    pull = await gw.open_pr(
        branch=job.branch,
        title=pr_title(title),
        body=pr_body(job.issue.number, summary, max_chars),
    )
    if pull is not None:
        await job.record.update(pr_number=pull.number)


async def await_ci(job: WorkerJob, head_sha: str) -> tuple[str, FollowUp | None]:
    ctx = job.ctx
    if ctx.gateway.dry_run:
        return DRY_RUN_CI, None
    limits = ctx.settings.limits
    status = await wait_for_ci(
        ctx.gateway,
        head_sha,
        required=ctx.required_checks,
        poll_s=limits.ci_poll_seconds,
        timeout_s=limits.ci_timeout_minutes * 60.0,
        grace_s=limits.ci_grace_minutes * 60.0,
        sleep=ctx.deps.sleep,
        clock=ctx.deps.clock,
    )
    if status.state is CiState.SUCCESS:
        return status.state.value, None
    if status.state is CiState.NONE:
        if ctx.settings.repo.require_ci:
            raise Escalation("no CI was reported for the change")
        return "no CI configured", None
    if status.state is CiState.PENDING:
        raise Escalation("CI did not finish in time")
    job.rounds.ci += 1
    if job.rounds.ci > limits.max_ci_rounds:
        raise Escalation(f"CI still fails after {limits.max_ci_rounds} fix rounds")
    await job.record.fire(TaskEvent.CI_FAILED, ci_rounds=job.rounds.ci)
    details = await _ci_details(job, status.failed_jobs)
    return status.state.value, FollowUp(FollowUpKind.CI_FAILED, details)


async def _ci_details(job: WorkerJob, failed_jobs: Sequence[FailedJob]) -> str:
    names = "\n".join(f"- {j.name}" for j in failed_jobs) or "- (unknown job)"
    ids = [j.job_id for j in failed_jobs if j.job_id is not None]
    logs = await job.ctx.gateway.failed_job_logs(ids, CI_LOG_CHARS) if ids else ""
    return f"Failed jobs:\n{names}\n\n{logs}".strip()


async def review(
    job: WorkerJob, check: PolicyCheck, worker_summary: str, ci_state: str
) -> FollowUp | None:
    gw, max_chars = job.ctx.gateway, job.ctx.settings.limits.max_comment_chars
    result = await review_change(job, check, worker_summary, ci_state)
    head = job.git.tip_sha
    summary = review_check_summary(result, max_chars)
    if result.verdict == "approve":
        await gw.set_review_check(sha=head, success=True, summary=summary)
        await job.record.fire(TaskEvent.REVIEW_APPROVED)
        return None
    await gw.set_review_check(sha=head, success=False, summary=summary)
    if result.verdict == "scope_too_large":
        raise Escalation(f"the reviewer judged the change too large: {result.summary}")
    job.rounds.review += 1
    _check_review_rounds(job)
    await job.record.fire(TaskEvent.REVIEW_CHANGES, review_rounds=job.rounds.review)
    return FollowUp(FollowUpKind.REVIEW_CHANGES, review_text(result))
