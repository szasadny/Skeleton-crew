import asyncio
import logging
from collections.abc import Sequence

from skeleton_crew.agents.prompting import PromptName
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.label_policy import refine_done_change
from skeleton_crew.domain.schemas import RefineResult
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_keys import refine_key
from skeleton_crew.domain.task_state import RefineState
from skeleton_crew.domain.trust_policy import IssueComment
from skeleton_crew.github.gateway import IssueRef
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.readonly import readonly_checkout, remove_checkout
from skeleton_crew.pipeline.sessions import Sandbox, remove_sandbox, run_agent, start_sandbox
from skeleton_crew.pipeline.task_record import RefineTaskRecord
from skeleton_crew.pipeline.texts import REFINE_RESERVED_CHARS, refine_comment
from skeleton_crew.runtime.workspace import task_paths

ERROR_CHARS = 1000

log = logging.getLogger(__name__)


async def run_refine(
    ctx: RunContext, task_id: int, issue: IssueRef, comments: Sequence[IssueComment]
) -> None:
    record = RefineTaskRecord(ctx.deps.sessionmaker, task_id)
    key = refine_key(task_id)
    paths = task_paths(ctx.deps.config.tasks_dir, key)
    sandbox: Sandbox | None = None
    error: str | None = None
    try:
        await record.set_state(RefineState.REFINING)
        await readonly_checkout(ctx, paths)
        sandbox = await start_sandbox(
            ctx,
            task_key=key,
            role=Role.REFINER,
            workspace=paths.agent_repo,
            writable=False,
            inbox=paths.inbox,
            transcripts=paths.transcripts,
        )
        error = await _refine(ctx, record, sandbox, issue, comments)
    except Exception as exc:
        log.exception("refine of #%d failed", issue.number)
        error = f"error: {type(exc).__name__}"
    finally:
        await remove_sandbox(ctx, sandbox)
        await asyncio.to_thread(remove_checkout, paths)
    await _finish(record, error)


async def _refine(
    ctx: RunContext,
    record: RefineTaskRecord,
    sandbox: Sandbox,
    issue: IssueRef,
    comments: Sequence[IssueComment],
) -> str | None:
    reply = await run_agent(
        ctx,
        sandbox,
        role=Role.REFINER,
        prompt=PromptName.REFINER,
        context={
            "issue": issue,
            "comments": comments,
            "max_chars": ctx.settings.limits.max_comment_chars - REFINE_RESERVED_CHARS,
        },
        output=RefineResult,
    )
    await record.update(cost_usd=reply.outcome.cost_usd, session_id=reply.outcome.session_id)
    if reply.result is None:
        return f"refiner session failed: {reply.error}"
    gw = ctx.gateway
    try:
        body = refine_comment(reply.result, ctx.settings.limits.max_comment_chars)
        await gw.comment(issue.number, body)
        await gw.change_labels(refine_done_change(issue.number, gw.labels))
    except PolicyViolation as exc:
        return f"blocked: {exc}"
    return None


async def _finish(record: RefineTaskRecord, error: str | None) -> None:
    try:
        if error is None:
            await record.set_state(RefineState.DONE)
        else:
            await record.set_state(RefineState.FAILED, error=error[:ERROR_CHARS])
    except Exception:
        log.exception("could not record the end of refine task %d", record.task_id)
