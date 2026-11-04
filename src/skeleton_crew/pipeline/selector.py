import asyncio
from collections.abc import Sequence

from skeleton_crew.agents.prompting import PromptName
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.schemas import SelectionResult, check_selection
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_keys import selector_key
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.readonly import readonly_checkout, remove_checkout
from skeleton_crew.pipeline.run_setup import Candidate
from skeleton_crew.pipeline.sessions import Sandbox, remove_sandbox, run_agent, start_sandbox
from skeleton_crew.runtime.workspace import task_paths


def _candidate_context(candidates: Sequence[Candidate]) -> list[dict[str, object]]:
    return [
        {
            "number": c.issue.number,
            "title": c.issue.title,
            "body": c.issue.body,
            "comments": list(c.comments),
        }
        for c in candidates
    ]


async def select_issues(
    ctx: RunContext, candidates: Sequence[Candidate], quota: int
) -> SelectionResult:
    if not candidates or quota <= 0:
        return SelectionResult()
    key = selector_key(ctx.run_id)
    paths = task_paths(ctx.deps.config.tasks_dir, key)
    sandbox: Sandbox | None = None
    try:
        await readonly_checkout(ctx, paths)
        sandbox = await start_sandbox(
            ctx,
            task_key=key,
            role=Role.SELECTOR,
            workspace=paths.agent_repo,
            writable=False,
            inbox=paths.inbox,
            transcripts=paths.transcripts,
        )
        reply = await run_agent(
            ctx,
            sandbox,
            role=Role.SELECTOR,
            prompt=PromptName.SELECTOR,
            context={"candidates": _candidate_context(candidates), "quota": quota},
            output=SelectionResult,
        )
    finally:
        await remove_sandbox(ctx, sandbox)
        await asyncio.to_thread(remove_checkout, paths)
    if reply.result is None:
        raise PolicyViolation(f"selector session failed: {reply.error}")
    return check_selection(reply.result, [c.issue.number for c in candidates], quota)
