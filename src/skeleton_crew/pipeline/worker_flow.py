import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta

from skeleton_crew.agents.prompting import PromptName
from skeleton_crew.domain.branch_policy import branch_name
from skeleton_crew.domain.contestable_checks import describe_checks
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.schemas import WorkerResult
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import TERMINAL_TASK_STATES, TaskEvent, TaskKind
from skeleton_crew.domain.trust_policy import IssueComment
from skeleton_crew.github.gateway import CiState, FailedJob, IssueRef
from skeleton_crew.pipeline.ci_watcher import wait_for_ci
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.escalation import Escalation, escalate_issue
from skeleton_crew.pipeline.repo_commands import run_repo_command
from skeleton_crew.pipeline.sessions import remove_sandbox, run_agent, start_sandbox
from skeleton_crew.pipeline.task_record import WorkerTaskRecord
from skeleton_crew.pipeline.texts import (
    CARRIED_REVIEW,
    COMMIT_AUTHOR,
    CONTINUED_SUMMARY,
    MERGE_SUMMARY,
    continued_comment,
    merge_message,
    new_issue_text,
    pr_body,
    pr_title,
    review_check_summary,
    review_text,
)
from skeleton_crew.pipeline.worker_job import WorkerJob, remove_work_dirs
from skeleton_crew.pipeline.worker_policy import PolicyCheck, check_change, inspect_commit
from skeleton_crew.pipeline.worker_review import review_change
from skeleton_crew.runtime.workspace import (
    checkout_commit,
    commit_tree,
    make_agent_copy,
    merge_trees,
    same_change,
    task_paths,
    tree_of,
)

CI_LOG_CHARS = 20_000
REASON_CHARS = 1000
DRY_RUN_CI = "skipped (dry run, nothing was pushed)"
CONFLICTS_MESSAGE = "Unresolved merge (local only)"

log = logging.getLogger(__name__)

type FollowUp = tuple[str, str]


class Skip(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


type Work = Callable[[WorkerJob], Awaitable[None]]


def worker_key(task_id: int) -> str:
    return f"worker-{task_id}"


async def run_worker(
    ctx: RunContext, task_id: int, issue: IssueRef, comments: Sequence[IssueComment]
) -> None:
    key = worker_key(task_id)
    job = WorkerJob(
        ctx=ctx,
        record=WorkerTaskRecord(ctx.deps.sessionmaker, task_id),
        issue=issue,
        comments=comments,
        key=key,
        paths=task_paths(ctx.deps.config.tasks_dir, key),
        branch=branch_name(issue.number, issue.title),
    )
    await run_job(job, _work)


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
            await job.record.fire(TaskEvent.ABORT, escalation_reason=outcome.reason)
            return
        if isinstance(outcome, Escalation):
            reason = outcome.reason[:REASON_CHARS]
            blocked = await escalate_issue(job.ctx, job.issue.number, reason)
            if blocked is None:
                await job.record.fire(TaskEvent.ESCALATE, escalation_reason=reason)
                return
            reason = f"{reason} ({blocked})"[:REASON_CHARS]
        else:
            reason = f"error: {type(outcome).__name__}"
        await job.record.fire(TaskEvent.ERROR, escalation_reason=reason)
    except Exception:
        log.exception("could not record the end of the worker for #%d", job.issue.number)


async def _prepare(job: WorkerJob) -> None:
    await job.record.fire(TaskEvent.PREPARED, branch=job.branch)
    job.base_sha = job.patch_base_sha = job.tip_sha = await job.ctx.gateway.clone(
        job.paths.controller_repo
    )
    await _start_worker_sandbox(job)
    await job.record.fire(TaskEvent.PREPARED, base_sha=job.base_sha)


async def _start_worker_sandbox(job: WorkerJob) -> None:
    ctx, paths = job.ctx, job.paths
    if job.on_existing_branch:
        await checkout_commit(paths.controller_repo, job.patch_base_sha)
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


async def _work(job: WorkerJob) -> None:
    head = await job.ctx.gateway.branch_head(job.branch)
    if head is None:
        await _prepare(job)
        await iterate(job, None)
        return
    job.remote_sha, job.continues_branch = head, True
    await job.record.fire(TaskEvent.PREPARED, branch=job.branch)
    await update_branch(job, reviewed=False)


async def update_branch(job: WorkerJob, reviewed: bool) -> None:
    ctx, repo = job.ctx, job.paths.controller_repo
    base = await ctx.gateway.clone(repo)
    head = await ctx.gateway.fetch_branch(repo, job.branch)
    if head != job.remote_sha:
        raise Skip("the branch changed while the update was prepared")
    job.base_sha = base
    attempt = await merge_trees(repo, head, base)
    if job.continues_branch and attempt.tree == await tree_of(repo, base):
        job.continues_branch = False
        job.patch_base_sha = job.tip_sha = base
        await _start_worker_sandbox(job)
        await job.record.fire(TaskEvent.PREPARED, base_sha=base)
        await iterate(job, None)
        return
    if job.continues_branch:
        await ctx.gateway.comment(job.issue.number, continued_comment(job.branch))
    if attempt.conflicts:
        job.patch_base_sha = await commit_tree(
            repo, tree=attempt.tree, parents=[head], message=CONFLICTS_MESSAGE, author=COMMIT_AUTHOR
        )
        job.tip_sha, job.merge_parents, job.conflicts = head, (head, base), attempt.conflicts
        await job.record.fire(TaskEvent.PREPARED, base_sha=base)
        await iterate(job, None)
        return
    if job.continues_branch and attempt.tree == await tree_of(repo, head):
        merged = head
    else:
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
        summary = CONTINUED_SUMMARY if job.continues_branch else MERGE_SUMMARY
        follow_up = await review(job, check, summary, ci_state)
        if follow_up is None:
            return
    await iterate(job, follow_up)


async def iterate(job: WorkerJob, follow_up: FollowUp | None) -> None:
    while True:
        result = await _code(job, follow_up)
        await job.record.fire(TaskEvent.CODE_DONE)
        follow_up = await _run_checks(job)
        if follow_up is not None:
            continue
        check = await check_change(job, result)
        if check.violations or check.head_sha is None:
            job.policy_rounds += 1
            _check_review_rounds(job)
            await job.record.fire(TaskEvent.POLICY_FAILED, policy_rounds=job.policy_rounds)
            follow_up = ("policy_failed", "\n".join(f"- {v}" for v in check.violations))
            continue
        job.tip_sha = check.head_sha
        job.merge_parents = ()
        await job.record.fire(TaskEvent.POLICY_PASSED)
        await publish(job, result, check.head_sha)
        ci_state, follow_up = await await_ci(job, check.head_sha)
        if follow_up is not None:
            continue
        await job.record.fire(TaskEvent.CI_PASSED)
        follow_up = await review(job, check, result.summary, ci_state)
        if follow_up is None:
            return


async def _run_checks(job: WorkerJob) -> FollowUp | None:
    command = job.ctx.settings.repo.check_command
    # While resolving a merge only conflicted files may change; CI reports the rest.
    if not command or job.merge_parents:
        return None
    checks = await run_repo_command(job, command, "check")
    if checks.ok:
        return None
    job.ci_rounds += 1
    limit = job.ctx.settings.limits.max_ci_rounds
    if job.ci_rounds > limit:
        raise Escalation(f"the check command still fails after {limit} fix rounds")
    await job.record.fire(TaskEvent.POLICY_FAILED, ci_rounds=job.ci_rounds)
    return "check_failed", f"`{command}` failed ({checks.status}):\n\n{checks.output_tail}".strip()


def _prompt(job: WorkerJob, follow_up: FollowUp | None) -> tuple[PromptName, dict[str, object]]:
    if follow_up is not None and job.session_id is not None:
        return PromptName.WORKER_FOLLOWUP, {"kind": follow_up[0], "details": follow_up[1]}
    if job.on_existing_branch and job.session_id is None:
        update: dict[str, object] = {
            "conflicts": list(job.conflicts),
            "kind": None,
            "details": None,
        }
        if follow_up is not None:
            update |= {"kind": follow_up[0], "details": follow_up[1]}
        base = job.ctx.settings.repo.base_branch
        return PromptName.WORKER_UPDATE, _initial_context(job) | update | {"base_branch": base}
    if follow_up is None:
        return PromptName.WORKER, _initial_context(job)
    raise Escalation("the worker session cannot be resumed")


async def _code(job: WorkerJob, follow_up: FollowUp | None) -> WorkerResult:
    ctx = job.ctx
    prompt, context = _prompt(job, follow_up)
    if job.sandbox is None:
        await _start_worker_sandbox(job)
    if job.sandbox is None:
        raise RuntimeError("worker container is not running")
    if job.kind is TaskKind.PR_UPDATE:
        job.kind = TaskKind.PR_FIX
        await job.record.update(kind=job.kind)
    reply = await run_agent(
        ctx,
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
        if job.new_issues >= limits.max_new_issues_per_worker:
            log.info("new-issue cap reached for #%d", job.issue.number)
            return
        job.new_issues += 1
        title, body = new_issue_text(new_issue, job.issue.number, limits.max_comment_chars)
        try:
            await job.ctx.gateway.create_hitl_issue(title, body)
        except PolicyViolation as exc:
            log.warning("new issue from #%d was blocked: %s", job.issue.number, exc)


def _check_review_rounds(job: WorkerJob) -> None:
    allowed = job.ctx.settings.limits.max_review_rounds
    if job.policy_rounds + job.review_rounds > allowed:
        raise Escalation(f"no acceptable change after {allowed} policy or review rounds")


async def publish(job: WorkerJob, result: WorkerResult | None, head_sha: str) -> None:
    gw, max_chars = job.ctx.gateway, job.ctx.settings.limits.max_comment_chars
    await gw.push_branch(job.paths.controller_repo, job.branch, head_sha, job.remote_sha)
    job.remote_sha = head_sha
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
    job.ci_rounds += 1
    if job.ci_rounds > limits.max_ci_rounds:
        raise Escalation(f"CI still fails after {limits.max_ci_rounds} fix rounds")
    await job.record.fire(TaskEvent.CI_FAILED, ci_rounds=job.ci_rounds)
    return status.state.value, ("ci_failed", await _ci_details(job, status.failed_jobs))


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
    head = job.tip_sha
    summary = review_check_summary(result, max_chars)
    if result.verdict == "approve":
        await gw.set_review_check(sha=head, success=True, summary=summary)
        await job.record.fire(TaskEvent.REVIEW_APPROVED)
        return None
    await gw.set_review_check(sha=head, success=False, summary=summary)
    if result.verdict == "scope_too_large":
        raise Escalation(f"the reviewer judged the change too large: {result.summary}")
    job.review_rounds += 1
    _check_review_rounds(job)
    await job.record.fire(TaskEvent.REVIEW_CHANGES, review_rounds=job.review_rounds)
    return ("review_changes", review_text(result))
