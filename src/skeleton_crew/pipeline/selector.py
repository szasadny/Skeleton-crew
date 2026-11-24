from collections.abc import Sequence

from skeleton_crew.agents.prompting import PromptName
from skeleton_crew.domain.schemas import SelectionResult, check_selection
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_keys import selector_key
from skeleton_crew.pipeline.candidates import Candidate
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.sandbox import readonly_sandbox, work_dirs
from skeleton_crew.pipeline.sessions import AgentCall, require_result, run_agent


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
    async with work_dirs(ctx.docs_clone):
        if not candidates or quota <= 0:
            return SelectionResult()
        selection = await _run_selector(ctx, candidates, quota)
    return check_selection(selection, [c.issue.number for c in candidates], quota)


async def _run_selector(
    ctx: RunContext, candidates: Sequence[Candidate], quota: int
) -> SelectionResult:
    context = {
        "candidates": _candidate_context(candidates),
        "quota": quota,
        "max_diff_lines": ctx.settings.limits.max_diff_lines,
    }
    key = selector_key(ctx.run_id)
    async with readonly_sandbox(ctx, key, Role.SELECTOR, ctx.docs_clone) as box:
        reply = await run_agent(ctx, box, AgentCall(PromptName.SELECTOR, context, SelectionResult))
    return require_result(reply, "selector", ctx.halted)
