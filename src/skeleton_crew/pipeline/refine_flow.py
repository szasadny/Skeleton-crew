import logging
from collections.abc import Sequence

from skeleton_crew.agents.prompting import PromptName
from skeleton_crew.domain.label_policy import refine_done_change
from skeleton_crew.domain.schemas import RefineResult
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_keys import refine_key
from skeleton_crew.domain.task_state import RefineState
from skeleton_crew.domain.trust_policy import IssueComment
from skeleton_crew.github.gateway import IssueRef
from skeleton_crew.pipeline.candidates import Candidate
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.escalation import comment_and_relabel
from skeleton_crew.pipeline.outcomes import JobStop, Skip, error_note
from skeleton_crew.pipeline.sandbox import Sandbox, readonly_sandbox, work_dirs
from skeleton_crew.pipeline.sessions import AgentCall, run_agent, settle
from skeleton_crew.pipeline.task_record import RefineTaskRecord
from skeleton_crew.pipeline.texts import REFINE_RESERVED_CHARS, refine_comment

log = logging.getLogger(__name__)


async def run_refine(ctx: RunContext, record: RefineTaskRecord, candidate: Candidate) -> None:
    issue, comments = candidate.issue, candidate.comments
    try:
        stop = await _run(ctx, record, issue, comments)
    except Exception as exc:
        log.exception("refine of #%d failed", issue.number)
        stop = JobStop(error_note(exc))
    await _finish(record, stop)


async def _run(
    ctx: RunContext, record: RefineTaskRecord, issue: IssueRef, comments: Sequence[IssueComment]
) -> JobStop | None:
    key = refine_key(record.task_id)
    paths = ctx.task_paths(key)
    try:
        async with work_dirs(paths):
            await record.update(state=RefineState.REFINING)
            await ctx.gateway.clone(paths.controller_repo)
            async with readonly_sandbox(ctx, key, Role.REFINER, paths) as box:
                return await _refine(ctx, record, box, issue, comments)
    except JobStop as stop:
        return stop


async def _refine(
    ctx: RunContext,
    record: RefineTaskRecord,
    sandbox: Sandbox,
    issue: IssueRef,
    comments: Sequence[IssueComment],
) -> JobStop | None:
    context = {
        "issue": issue,
        "comments": comments,
        "max_chars": ctx.settings.limits.max_comment_chars - REFINE_RESERVED_CHARS,
    }
    reply = await run_agent(ctx, sandbox, AgentCall(PromptName.REFINER, context, RefineResult))
    result = await settle(ctx, record, reply, "refiner", session_id=reply.outcome.session_id)
    body = refine_comment(result, ctx.settings.limits.max_comment_chars)
    blocked = await comment_and_relabel(ctx.gateway, issue.number, body, refine_done_change)
    return None if blocked is None else JobStop(f"blocked: {blocked}")


async def _finish(record: RefineTaskRecord, stop: JobStop | None) -> None:
    try:
        if stop is None:
            await record.update(state=RefineState.DONE)
        else:
            state = RefineState.ABORTED if isinstance(stop, Skip) else RefineState.FAILED
            await record.end(state, stop.reason)
    except Exception:
        log.exception("could not record the end of refine task %d", record.task_id)
