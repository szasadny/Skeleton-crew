import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from skeleton_crew.agents.backends._shared import (
    ANTHROPIC_KEY,
    ENDED_WITH,
    REPORTED_ERROR,
    AuthMode,
    CliBackend,
    CliFiles,
    Credential,
    SessionReport,
    auth_mode_option,
    blocked_tools,
    missing_outcome,
    number,
    read_stream,
    safe_reason,
    safe_session_id,
    session_outcome,
    stdin_command,
)
from skeleton_crew.agents.base import BackendSpec, SessionOutcome, SessionRequest

INTERACTIVE_TOOLS = (
    "AskUserQuestion",
    "EnterPlanMode",
    "ExitPlanMode",
    "CronCreate",
    "CronDelete",
    "RemoteTrigger",
    "ScheduleWakeup",
    "PushNotification",
)
WEB_TOOLS = ("WebSearch", "WebFetch")
# Aliases follow the latest model; full ids pin one.
MODELS = (
    "opus",
    "sonnet",
    "haiku",
    "fable",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "claude-fable-5-1",
)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CREDENTIALS = {
    AuthMode.SUBSCRIPTION: Credential("claude_oauth_token", "CLAUDE_CODE_OAUTH_TOKEN"),
    AuthMode.API_KEY: ANTHROPIC_KEY,
}
_BASE_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}


def _is_limit_signal(event: dict[str, Any]) -> bool:
    kind = event.get("type")
    if kind == "assistant":
        return event.get("error") == "rate_limit"
    if kind != "rate_limit_event":
        return False
    info = event.get("rate_limit_info")
    return isinstance(info, dict) and info.get("status") == "rejected"


@dataclass
class _Stream:
    result: dict[str, Any] | None = None
    limited: bool = False

    def add(self, event: dict[str, Any]) -> None:
        if event.get("type") == "result":
            self.result = event
        elif _is_limit_signal(event):
            self.limited = True


def _answer(result: dict[str, Any]) -> str | None:
    value = result.get("structured_output")
    if isinstance(value, dict):
        return json.dumps(value)
    answer = result.get("result")
    return answer if isinstance(answer, str) else None


def _failure(result: dict[str, Any]) -> str | None:
    subtype = result.get("subtype")
    if subtype != "success":
        return ENDED_WITH.format(safe_reason(subtype) or "unknown")
    return REPORTED_ERROR if result.get("is_error") is not False else None


def _report(result: dict[str, Any], limited: bool) -> SessionReport:
    return SessionReport(
        session_id=safe_session_id(result.get("session_id")),
        answer=_answer(result),
        failure=_failure(result),
        cost=number(result.get("total_cost_usd")),
        turns=int(number(result.get("num_turns"))),
        stop_reason=result.get("terminal_reason"),
        limited=limited,
        stops_without_session=True,
    )


class ClaudeCodeBackend(CliBackend):
    name = "claude_code"
    display_name = "Claude Code"
    auto_loaded = ("CLAUDE.md", ".claude/CLAUDE.md")
    base_env = _BASE_ENV
    credentials = _CREDENTIALS

    def egress_domains(self) -> tuple[str, ...]:
        return (ANTHROPIC_KEY.host,)

    def command(self, req: SessionRequest) -> list[str]:
        disallowed = blocked_tools(INTERACTIVE_TOOLS, WEB_TOOLS, web_tools=req.agent.web_tools)
        args = [
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            req.agent.model,
            "--effort",
            req.agent.effort,
            "--max-turns",
            str(req.max_turns),
            "--json-schema",
            req.json_schema,
            "--append-system-prompt-file",
            req.rules_path_in_container,
            "--permission-prompts",
            "none",
            "--dangerously-skip-permissions",
            "--strict-mcp-config",
            "--disallowedTools",
            ",".join(disallowed),
        ]
        if req.resume_session_id is not None:
            args += ["--resume", req.resume_session_id]
        if self._auth_mode is AuthMode.API_KEY and req.max_budget_usd is not None:
            args += ["--max-budget-usd", str(req.max_budget_usd)]
        return stdin_command("claude", args, CliFiles(req.prompt_path_in_container))

    def parse(self, stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome:
        stream = read_stream(stdout_path, _Stream())
        if stream.result is None:
            missing = missing_outcome("result", exit_code, timed_out)
            return replace(missing, usage_limited=stream.limited)
        return session_outcome(_report(stream.result, stream.limited), exit_code, timed_out)


SPEC = BackendSpec(
    name=ClaudeCodeBackend.name,
    display_name=ClaudeCodeBackend.display_name,
    models=MODELS,
    efforts=EFFORTS,
    factory=ClaudeCodeBackend.create,
    options={"auth_mode": auth_mode_option(AuthMode.SUBSCRIPTION)},
    credentials=ClaudeCodeBackend.credential_names(),
)
