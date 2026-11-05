import asyncio
from collections.abc import Sequence

from skeleton_crew.agents.prompting import PromptName
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.schemas import SelectionResult, check_selection
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_keys import selector_key
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.readonly import readonly_copy_of_clone, remove_checkout
from skeleton_crew.pipeline.run_setup import Candidate
from skeleton_crew.pipeline.sessions import Sandbox, remove_sandbox, run_agent, start_sandbox
from skeleton_crew.runtime.workspace import TaskPaths, task_paths


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
    paths = task_paths(ctx.deps.config.tasks_dir, selector_key(ctx.run_id))
    try:
        if not candidates or quota <= 0:
            return SelectionResult()
        selection = await _run_selector(ctx, paths, candidates, quota)
    finally:
        await asyncio.to_thread(remove_checkout, paths)
    return check_selection(selection, [c.issue.number for c in candidates], quota)


async def _run_selector(
    ctx: RunContext, paths: TaskPaths, candidates: Sequence[Candidate], quota: int
) -> SelectionResult:
    sandbox: Sandbox | None = None
    try:
        # Run setup already cloned the base branch here to collect the repository docs.
        await readonly_copy_of_clone(paths)
        sandbox = await start_sandbox(
            ctx,
            task_key=paths.root.name,
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
    if reply.result is None:
        raise PolicyViolation(f"selector session failed: {reply.error}")
    return reply.result
