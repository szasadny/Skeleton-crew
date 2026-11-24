import asyncio
import logging

from skeleton_crew.agents.prompting import PromptName
from skeleton_crew.domain.classifier_policy import injection_reason
from skeleton_crew.domain.contest_policy import describe_checks, objection_key
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.job_policy import is_repo_output, may_report_new_issue, worker_finished
from skeleton_crew.domain.schemas import WorkerResult
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_keys import RepoCommand
from skeleton_crew.pipeline.classify import screen_output
from skeleton_crew.pipeline.outcomes import Escalation
from skeleton_crew.pipeline.repo_commands import run_repo_command
from skeleton_crew.pipeline.sandbox import Sandbox, start_sandbox
from skeleton_crew.pipeline.sessions import AgentCall, run_agent, settle
from skeleton_crew.pipeline.texts import (
    WORKER_NOT_RESUMABLE,
    new_issue_text,
    setup_failed,
    tag_blockers,
    worker_gave_up,
)
from skeleton_crew.pipeline.worker_job import FollowUp, WorkerJob
from skeleton_crew.runtime.controller_git import checkout_commit, make_agent_copy

log = logging.getLogger(__name__)


async def ensure_worker_sandbox(job: WorkerJob) -> Sandbox:
    progress = job.progress
    if progress.sandbox is not None:
        return progress.sandbox
    ctx, paths = job.ctx, job.paths
    if job.on_existing_branch:
        await checkout_commit(paths.controller_repo, job.git.patch_base_sha)
    await asyncio.to_thread(make_agent_copy, paths)
    progress.sandbox = await start_sandbox(ctx, job.key, Role.WORKER, paths)
    await job.record.update(container_id=progress.sandbox.handle.id)
    command = ctx.settings.repo.setup_command
    if command:
        setup = await run_repo_command(job, command, RepoCommand.SETUP)
        if not setup.ok:
            raise Escalation(setup_failed(setup.status))
    return progress.sandbox


async def code(job: WorkerJob, follow_up: FollowUp | None) -> WorkerResult:
    if follow_up is not None and is_repo_output(follow_up.kind):
        problem = await screen_output(job.ctx, follow_up.details)
        if problem is not None:
            raise Escalation(injection_reason(f"{follow_up.kind.value} output", problem))
    prompt, context = _prompt(job, follow_up)
    sandbox = await ensure_worker_sandbox(job)
    progress = job.progress
    resume = progress.session_id if prompt is PromptName.WORKER_FOLLOWUP else None
    call = AgentCall(prompt, context, WorkerResult, resume=resume, effort=job.effort)
    reply = await run_agent(job.ctx, sandbox, call)
    if reply.outcome.session_id is not None:
        progress.session_id = reply.outcome.session_id
    result = await settle(job.ctx, job.record, reply, "worker", session_id=progress.session_id)
    await job.record.add_blockers(tag_blockers(Role.WORKER, result.blockers))
    await _report_new_issues(job, result)
    progress.objections |= {objection_key(o): o.reason for o in result.objections}
    if not worker_finished(result.status):
        raise Escalation(worker_gave_up(result.status, result.note))
    return result


def _follow_up_context(follow_up: FollowUp) -> dict[str, object]:
    return {"kind": follow_up.kind.value, "details": follow_up.details}


def _prompt(job: WorkerJob, follow_up: FollowUp | None) -> tuple[PromptName, dict[str, object]]:
    session_id = job.progress.session_id
    if follow_up is not None and session_id is not None:
        return PromptName.WORKER_FOLLOWUP, _follow_up_context(follow_up)
    if job.on_existing_branch and session_id is None:
        update: dict[str, object] = {
            "conflicts": list(job.git.conflicts),
            "base_branch": job.base_branch,
        }
        if follow_up is not None:
            update |= _follow_up_context(follow_up)
        return PromptName.WORKER_UPDATE, _initial_context(job) | update
    if follow_up is None:
        return PromptName.WORKER, _initial_context(job)
    raise Escalation(WORKER_NOT_RESUMABLE)


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
        if not may_report_new_issue(job.progress.new_issues, limits.max_new_issues_per_worker):
            log.info("new-issue cap reached for #%d", job.issue.number)
            return
        job.progress.new_issues += 1
        title, body = new_issue_text(new_issue, job.issue.number, limits.max_comment_chars)
        try:
            await job.ctx.gateway.create_hitl_issue(title, body)
        except PolicyViolation as exc:
            log.warning("new issue from #%d was blocked: %s", job.issue.number, exc)
