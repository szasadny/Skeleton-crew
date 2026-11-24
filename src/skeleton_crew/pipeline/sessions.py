import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from skeleton_crew.agents.base import ExecOutcome, Executor, SessionOutcome, SessionRequest
from skeleton_crew.agents.prompting import PromptName, render_prompt
from skeleton_crew.agents.session import run_session
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.schemas import json_schema, parse_output
from skeleton_crew.domain.session_policy import SessionBudget, may_continue, remaining_budget
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_keys import SESSION_SUFFIX
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.outcomes import SessionFailed, UsageLimitReached
from skeleton_crew.pipeline.sandbox import INBOX, RULES_FILE, Sandbox, update_inbox
from skeleton_crew.pipeline.texts import session_failed
from skeleton_crew.runtime.containers import AGENT_USER, WORKSPACE, ContainerHandle, ExecRequest
from skeleton_crew.runtime.task_dirs import next_numbered

PROMPT_FILE = "prompt.md"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentCall[T: BaseModel]:
    prompt: PromptName
    context: Mapping[str, object]
    output: type[T]
    resume: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class AgentReply[T: BaseModel]:
    outcome: SessionOutcome
    result: T | None
    error: str | None


class TaskRecorder(Protocol):
    async def add_cost(self, cost_usd: float, **fields: object) -> None: ...


def container_executor(ctx: RunContext, handle: ContainerHandle) -> Executor:
    async def execute(
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        timeout_s: float,
        stdout_path: Path,
    ) -> ExecOutcome:
        request = ExecRequest(argv, AGENT_USER, cwd, timeout_s, env=env, stdout_path=stdout_path)
        result = await ctx.deps.runtime.exec(handle, request)
        stderr = result.stderr.decode(errors="replace")
        return ExecOutcome(result.exit_code, result.timed_out, stderr)

    return execute


def _request[T: BaseModel](ctx: RunContext, role: Role, call: AgentCall[T]) -> SessionRequest:
    agent = ctx.settings.agents[role]
    if call.effort is not None:
        agent = agent.model_copy(update={"effort": call.effort})
    limits = ctx.settings.limits
    schema = json_schema(call.output)
    return SessionRequest(
        role=role,
        prompt=render_prompt(call.prompt, call.context, json_schema=schema),
        agent=agent,
        max_turns=limits.max_turns,
        timeout_s=limits.session_timeout_s,
        json_schema=schema,
        rules_path_in_container=f"{INBOX}/{RULES_FILE}",
        prompt_path_in_container=f"{INBOX}/{PROMPT_FILE}",
        cwd=WORKSPACE,
        resume_session_id=call.resume,
        max_budget_usd=limits.max_budget_usd_per_session,
    )


async def run_agent[T: BaseModel](
    ctx: RunContext, sandbox: Sandbox, call: AgentCall[T]
) -> AgentReply[T]:
    outcome = await _run_with_continues(ctx, sandbox, _request(ctx, sandbox.role, call))
    if outcome.usage_limited or not outcome.ok or outcome.structured_output is None:
        return AgentReply(outcome, None, _failure(outcome))
    try:
        return AgentReply(outcome, parse_output(call.output, outcome.structured_output), None)
    except PolicyViolation as exc:
        log.warning("%s output rejected: %s", sandbox.role.value, exc)
        return AgentReply(outcome, None, str(exc))


def require_result[T: BaseModel](reply: AgentReply[T], what: str, halted: asyncio.Event) -> T:
    if reply.outcome.usage_limited:
        if not halted.is_set():
            log.warning("%s session hit the agent usage limit, starting no new work", what)
        halted.set()
        raise UsageLimitReached()
    if reply.result is None:
        raise SessionFailed(session_failed(what, reply.error))
    return reply.result


async def settle[T: BaseModel](
    ctx: RunContext, record: TaskRecorder, reply: AgentReply[T], what: str, **fields: object
) -> T:
    await record.add_cost(reply.outcome.cost_usd, **fields)
    return require_result(reply, what, ctx.halted)


async def _session(ctx: RunContext, sandbox: Sandbox, request: SessionRequest) -> SessionOutcome:
    files = {PROMPT_FILE: request.prompt.encode("utf-8")}
    await asyncio.to_thread(update_inbox, sandbox.inbox, files)
    return await run_session(
        ctx.backends[request.agent.backend],
        container_executor(ctx, sandbox.handle),
        request,
        await asyncio.to_thread(next_numbered, sandbox.transcripts, sandbox.role, SESSION_SUFFIX),
    )


def _budget(request: SessionRequest) -> SessionBudget:
    return SessionBudget(request.max_turns, request.timeout_s, request.max_budget_usd)


async def _run_with_continues(
    ctx: RunContext, sandbox: Sandbox, request: SessionRequest
) -> SessionOutcome:
    started = ctx.deps.clock()
    outcome = await _session(ctx, sandbox, request)
    cost, turns = outcome.cost_usd, outcome.turns
    prompt = render_prompt(PromptName.CONTINUE, {}, json_schema=request.json_schema)
    for _ in range(ctx.settings.limits.max_continues):
        if not may_continue(outcome.stopped_early, outcome.session_id, outcome.usage_limited):
            break
        elapsed_s = (ctx.deps.clock() - started).total_seconds()
        left = remaining_budget(_budget(request), cost, turns, elapsed_s)
        if left is None:
            log.info("%s session used up its limits, not continuing", request.role)
            break
        log.info("%s session stopped early (%s), continuing", request.role, outcome.stopped_early)
        resumed = replace(
            request,
            prompt=prompt,
            resume_session_id=outcome.session_id,
            max_turns=left.max_turns,
            timeout_s=left.timeout_s,
            max_budget_usd=left.max_budget_usd,
        )
        outcome = await _session(ctx, sandbox, resumed)
        cost, turns = cost + outcome.cost_usd, turns + outcome.turns
    return replace(outcome, cost_usd=cost, turns=turns)


def _failure(outcome: SessionOutcome) -> str:
    error = outcome.error or "no structured output"
    if outcome.stopped_early is None:
        return error
    return f"{error} (stopped early: {outcome.stopped_early})"
