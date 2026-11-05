import asyncio
import logging
import re
import secrets
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import BaseModel

from skeleton_crew.agents.base import ExecOutcome, Executor, SessionOutcome, SessionRequest
from skeleton_crew.agents.prompting import PromptName, render_prompt
from skeleton_crew.agents.session import run_session
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.schemas import json_schema, parse_output
from skeleton_crew.domain.settings import Role
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.runtime.containers import AgentContainerSpec, ContainerHandle, Mount

AGENT_USER = "agent"
WORKSPACE = "/workspace"
INBOX = "/skeleton-in"
RULES_FILE = "rules.md"
TASK_LABEL = "skeleton-crew.task"
ROLE_LABEL = "skeleton-crew.role"
_STDERR_TAIL_CHARS = 4000

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sandbox:
    handle: ContainerHandle
    transcripts: Path


@dataclass(frozen=True)
class AgentReply[T: BaseModel]:
    outcome: SessionOutcome
    result: T | None
    error: str | None


def proxy_env(proxy_url: str) -> dict[str, str]:
    no_proxy = "localhost,127.0.0.1"
    return {
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
    }


def container_spec(
    ctx: RunContext, *, task_key: str, role: Role, workspace: Path, writable: bool, inbox: Path
) -> AgentContainerSpec:
    config, limits = ctx.deps.config, ctx.settings.runtime
    return AgentContainerSpec(
        name=f"skeleton-crew-{task_key}-{role.value}-{secrets.token_hex(4)}",
        image=config.agent_image,
        network=config.agent_network,
        mounts=[
            Mount(workspace, WORKSPACE, read_only=not writable),
            Mount(inbox, INBOX, read_only=True),
        ],
        env=proxy_env(config.egress_proxy_url),
        cpus=limits.cpus,
        memory_gb=limits.memory_gb,
        pids=limits.pids,
        labels={TASK_LABEL: task_key, ROLE_LABEL: role.value},
    )


def write_inbox(inbox: Path, rules: str, files: Mapping[str, bytes]) -> None:
    if inbox.exists():
        shutil.rmtree(inbox)
    inbox.mkdir(parents=True)
    (inbox / RULES_FILE).write_text(rules, encoding="utf-8")
    update_inbox(inbox, files)


def update_inbox(inbox: Path, files: Mapping[str, bytes]) -> None:
    for name, content in files.items():
        (inbox / name).write_bytes(content)


async def start_sandbox(
    ctx: RunContext,
    *,
    task_key: str,
    role: Role,
    workspace: Path,
    writable: bool,
    inbox: Path,
    transcripts: Path,
    files: Mapping[str, bytes] | None = None,
) -> Sandbox:
    rules = ctx.rules[role]
    docs = ctx.repo_docs[ctx.settings.agents[role].backend]
    if docs:
        rules = f"{rules}\n{docs}"
    await asyncio.to_thread(write_inbox, inbox, rules, files or {})
    spec = container_spec(
        ctx, task_key=task_key, role=role, workspace=workspace, writable=writable, inbox=inbox
    )
    return Sandbox(await ctx.deps.runtime.start(spec), transcripts)


def container_executor(ctx: RunContext, handle: ContainerHandle) -> Executor:
    async def execute(
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        timeout_s: float,
        stdout_path: Path,
    ) -> ExecOutcome:
        result = await ctx.deps.runtime.exec(
            handle,
            argv,
            env=env,
            cwd=cwd,
            user=AGENT_USER,
            timeout_s=timeout_s,
            stdout_path=stdout_path,
        )
        tail = result.stderr.decode(errors="replace")[-_STDERR_TAIL_CHARS:]
        return ExecOutcome(result.exit_code, result.timed_out, tail)

    return execute


def next_numbered(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(rf"{re.escape(stem)}-(\d+){re.escape(suffix)}", re.ASCII)
    used = (int(m[1]) for path in directory.iterdir() if (m := pattern.fullmatch(path.name)))
    return directory / f"{stem}-{max(used, default=0) + 1}{suffix}"


def next_transcript(transcripts: Path, role: Role) -> Path:
    return next_numbered(transcripts, role.value, ".jsonl")


async def run_agent[T: BaseModel](
    ctx: RunContext,
    sandbox: Sandbox,
    *,
    role: Role,
    prompt: PromptName,
    context: Mapping[str, object],
    output: type[T],
    resume_session_id: str | None = None,
) -> AgentReply[T]:
    agent = ctx.settings.agents[role]
    limits = ctx.settings.limits
    schema = json_schema(output)
    request = SessionRequest(
        role=role,
        prompt=render_prompt(prompt, context, json_schema=schema),
        agent=agent,
        max_turns=limits.max_turns,
        timeout_s=limits.session_timeout_minutes * 60.0,
        json_schema=schema,
        rules_path_in_container=f"{INBOX}/{RULES_FILE}",
        cwd=WORKSPACE,
        resume_session_id=resume_session_id,
        max_budget_usd=limits.max_budget_usd_per_session,
    )
    outcome = await _continue_stopped(
        ctx, sandbox, request, await _session(ctx, sandbox, request), schema
    )
    if not outcome.ok or outcome.structured_output is None:
        return AgentReply(outcome, None, _failure(outcome))
    try:
        return AgentReply(outcome, parse_output(output, outcome.structured_output), None)
    except PolicyViolation as exc:
        log.warning("%s output rejected: %s", role.value, exc)
        return AgentReply(outcome, None, str(exc))


async def _session(ctx: RunContext, sandbox: Sandbox, request: SessionRequest) -> SessionOutcome:
    return await run_session(
        ctx.backends[request.agent.backend],
        container_executor(ctx, sandbox.handle),
        request,
        await asyncio.to_thread(next_transcript, sandbox.transcripts, request.role),
    )


async def _continue_stopped(
    ctx: RunContext,
    sandbox: Sandbox,
    request: SessionRequest,
    outcome: SessionOutcome,
    schema: str,
) -> SessionOutcome:
    cost, turns = outcome.cost_usd, outcome.turns
    prompt = render_prompt(PromptName.CONTINUE, {}, json_schema=schema)
    for _ in range(ctx.settings.limits.max_continues):
        if outcome.stopped_early is None or outcome.session_id is None:
            break
        log.info("%s session stopped early (%s), continuing", request.role, outcome.stopped_early)
        resumed = replace(request, prompt=prompt, resume_session_id=outcome.session_id)
        outcome = await _session(ctx, sandbox, resumed)
        cost, turns = cost + outcome.cost_usd, turns + outcome.turns
    return replace(outcome, cost_usd=cost, turns=turns)


def _failure(outcome: SessionOutcome) -> str:
    error = outcome.error or "no structured output"
    if outcome.stopped_early is None:
        return error
    return f"{error} (stopped early: {outcome.stopped_early})"


async def remove_sandbox(ctx: RunContext, sandbox: Sandbox | None) -> None:
    if sandbox is None:
        return
    try:
        await ctx.deps.runtime.remove(sandbox.handle)
    except Exception:
        log.exception("could not remove container %s", sandbox.handle.name)
