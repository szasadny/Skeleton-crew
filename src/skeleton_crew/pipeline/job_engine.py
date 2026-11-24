import asyncio
import logging
from collections.abc import Awaitable, Callable

from skeleton_crew.domain.job_policy import check_to_run, job_seconds_left
from skeleton_crew.domain.task_keys import RepoCommand
from skeleton_crew.domain.task_state import (
    TERMINAL_TASK_STATES,
    FollowUpKind,
    TaskEvent,
    cap_note,
)
from skeleton_crew.pipeline.branch_update import BaseMerge, prepare_clean_merge, prepare_conflicts
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.escalation import escalate_issue
from skeleton_crew.pipeline.outcomes import Escalation, JobStop, Skip, error_note
from skeleton_crew.pipeline.publish import await_ci, publish
from skeleton_crew.pipeline.repo_commands import run_repo_command
from skeleton_crew.pipeline.sandbox import remove_sandbox, remove_work_dirs
from skeleton_crew.pipeline.texts import worker_timeout_reason
from skeleton_crew.pipeline.worker_job import FollowUp, WorkerJob, WorkerReport, next_round
from skeleton_crew.pipeline.worker_policy import PassedCheck, PolicyCheck, check_change
from skeleton_crew.pipeline.worker_review import review
from skeleton_crew.pipeline.worker_session import code

log = logging.getLogger(__name__)

type Work = Callable[[WorkerJob], Awaitable[None]]


async def run_job(job: WorkerJob, work: Work) -> None:
    ctx, issue = job.ctx, job.issue
    outcome: Exception | None = None
    try:
        async with asyncio.timeout(_seconds_left(ctx)):
            await work(job)
    except TimeoutError:
        outcome = Escalation(worker_timeout_reason(job.record.state))
    except JobStop as exc:
        outcome = exc
    except Exception as exc:
        log.exception("worker for #%d failed", issue.number)
        outcome = exc
    finally:
        await remove_sandbox(ctx, job.progress.sandbox)
        await remove_sandbox(ctx, job.reviewer.sandbox)
        await asyncio.to_thread(remove_work_dirs, job.paths)
    if outcome is not None:
        await _finish_unsuccessful(job, outcome)


def _seconds_left(ctx: RunContext) -> float:
    limits = ctx.settings.limits
    return job_seconds_left(ctx.deps.clock(), limits.worker_timeout_hours, ctx.deadline)


async def _finish_unsuccessful(job: WorkerJob, outcome: Exception) -> None:
    if job.record.state in TERMINAL_TASK_STATES:
        return
    try:
        if isinstance(outcome, Skip):
            await job.record.end(TaskEvent.ABORT, outcome.reason)
            return
        if isinstance(outcome, Escalation):
            note = cap_note(outcome.reason)
            blocked = await escalate_issue(job.ctx, job.issue.number, note)
            if blocked is None:
                await job.record.end(TaskEvent.ESCALATE, note)
                return
            note = f"{note} ({blocked})"
        else:
            note = error_note(outcome)
        await job.record.end(TaskEvent.ERROR, note)
    except Exception:
        log.exception("could not record the end of the worker for #%d", job.issue.number)


async def iterate(job: WorkerJob, follow_up: FollowUp | None) -> None:
    while True:
        result = await code(job, follow_up)
        await job.record.fire(TaskEvent.CODE_DONE)
        follow_up = await _run_checks(job)
        if follow_up is not None:
            continue
        check = await check_change(job, result)
        passed = check.passed()
        if passed is None:
            follow_up = await _policy_failed(job, check)
            continue
        follow_up = await deliver(job, passed, WorkerReport.of(result))
        if follow_up is None:
            return


async def deliver(job: WorkerJob, check: PassedCheck, report: WorkerReport) -> FollowUp | None:
    job.git.committed(check.head_sha)
    await job.record.fire(TaskEvent.POLICY_PASSED)
    await publish(job, check.head_sha, report)
    ci = await await_ci(job, check.head_sha)
    if ci.follow_up is not None:
        return ci.follow_up
    await job.record.fire(TaskEvent.CI_PASSED)
    return await review(job, check, report, ci.result)


async def _run_checks(job: WorkerJob) -> FollowUp | None:
    command = check_to_run(job.ctx.settings.repo.check_command, job.git.resolving_merge)
    if command is None:
        return None
    checks = await run_repo_command(job, command, RepoCommand.CHECK)
    if checks.ok:
        return None
    await next_round(job, FollowUpKind.CHECK_FAILED)
    details = f"`{command}` failed ({checks.status}):\n\n{checks.output_tail}".strip()
    return FollowUp(FollowUpKind.CHECK_FAILED, details)


async def _policy_failed(job: WorkerJob, check: PolicyCheck) -> FollowUp:
    await next_round(job, FollowUpKind.POLICY_FAILED)
    return FollowUp(FollowUpKind.POLICY_FAILED, "\n".join(f"- {v}" for v in check.violations))


async def update_branch(job: WorkerJob, merge: BaseMerge) -> None:
    if merge.conflicts:
        await prepare_conflicts(job, merge)
        await iterate(job, None)
        return
    check = await prepare_clean_merge(job, merge)
    follow_up = await deliver(job, check, job.continued_report())
    if follow_up is not None:
        await iterate(job, follow_up)
