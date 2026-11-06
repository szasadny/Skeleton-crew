import json
import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from skeleton_crew.agents.base import (
    BackendSpec,
    OptionSpec,
    SessionOutcome,
    SessionRequest,
    failed_outcome,
)
from skeleton_crew.agents.output import extract_json_text

NAME = "claude_code"


class AuthMode(StrEnum):
    SUBSCRIPTION = "subscription"
    API_KEY = "api_key"


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


def disallowed_tools(*, web_tools: bool) -> str:
    return ",".join(INTERACTIVE_TOOLS if web_tools else (*INTERACTIVE_TOOLS, *WEB_TOOLS))


EGRESS_DOMAINS = ("api.anthropic.com",)
AUTO_LOADED_DOCS = ("CLAUDE.md", ".claude/CLAUDE.md")
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
    AuthMode.SUBSCRIPTION: ("claude_oauth_token", "CLAUDE_CODE_OAUTH_TOKEN"),
    AuthMode.API_KEY: ("anthropic_api_key", "ANTHROPIC_API_KEY"),
}
_BASE_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
# On stdin, because Linux caps a single argument at 128 KiB and prompts can be larger.
_PROMPT_FROM_STDIN = 'prompt="$1"; shift; exec claude "$@" < "$prompt"'
_SUBTYPE_RE = re.compile(r"^[a-z_]{1,40}$")
# The id is later passed to --resume, so it must never look like an option.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


def _last_result(stdout_path: Path) -> dict[str, Any] | None:
    result: dict[str, Any] | None = None
    try:
        with stdout_path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "result":
                    result = event
    except FileNotFoundError:
        return None
    return result


def _structured_output(result: dict[str, Any]) -> str | None:
    value = result.get("structured_output")
    if isinstance(value, dict):
        return json.dumps(value)
    answer = result.get("result")
    return extract_json_text(answer) if isinstance(answer, str) else None


class ClaudeCodeBackend:
    name = NAME

    def __init__(self, *, auth_mode: AuthMode, credentials: Mapping[str, SecretStr]) -> None:
        self._auth_mode = auth_mode
        self._credentials = dict(credentials)

    def egress_domains(self) -> tuple[str, ...]:
        return EGRESS_DOMAINS

    def auto_loaded_docs(self) -> tuple[str, ...]:
        return AUTO_LOADED_DOCS

    def _credential(self) -> tuple[str, str | None]:
        name, var = _CREDENTIALS[self._auth_mode]
        secret = self._credentials.get(name)
        value = secret.get_secret_value().strip() if secret is not None else ""
        return var, value or None

    def credential_problems(self) -> list[str]:
        _, value = self._credential()
        if value is None:
            setting = f"SC_AGENT_CREDENTIALS__{_CREDENTIALS[self._auth_mode][0].upper()}"
            return [f"Claude Code ({self._auth_mode.value} mode): {setting} is not set"]
        return []

    def env(self) -> dict[str, str]:
        var, value = self._credential()
        return {**_BASE_ENV, var: value} if value is not None else dict(_BASE_ENV)

    def command(self, req: SessionRequest) -> list[str]:
        argv = [
            "sh",
            "-c",
            _PROMPT_FROM_STDIN,
            "sh",
            req.prompt_path_in_container,
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
            disallowed_tools(web_tools=req.agent.web_tools),
        ]
        if req.resume_session_id is not None:
            argv += ["--resume", req.resume_session_id]
        if self._auth_mode is AuthMode.API_KEY and req.max_budget_usd is not None:
            argv += ["--max-budget-usd", str(req.max_budget_usd)]
        return argv

    def parse(self, stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome:
        result = _last_result(stdout_path)
        if result is None:
            missing = "timed out" if timed_out else f"no result in output (exit code {exit_code})"
            return failed_outcome(missing, timed_out=timed_out)
        session_id = result.get("session_id")
        subtype = result.get("subtype")
        structured = _structured_output(result)
        error: str | None = None
        stopped_early: str | None = None
        if timed_out:
            error = "timed out"
        elif subtype != "success":
            shown = (
                subtype if isinstance(subtype, str) and _SUBTYPE_RE.match(subtype) else "unknown"
            )
            error = f"session ended with {shown}"
        elif result.get("is_error") is not False:
            error = "session reported an error"
        elif exit_code != 0:
            error = f"exit code {exit_code}"
        elif structured is None:
            error = "no structured output"
            reason = result.get("terminal_reason")
            valid = isinstance(reason, str) and _SUBTYPE_RE.match(reason)
            stopped_early = reason if valid else "no_result"
        return SessionOutcome(
            session_id=(
                session_id
                if isinstance(session_id, str) and _SESSION_ID_RE.match(session_id)
                else None
            ),
            structured_output=structured,
            cost_usd=_number(result.get("total_cost_usd"), 0.0),
            turns=int(_number(result.get("num_turns"), 0)),
            ok=error is None,
            error=error,
            timed_out=timed_out,
            stopped_early=stopped_early,
        )


def _create(credentials: Mapping[str, SecretStr], options: Mapping[str, str]) -> ClaudeCodeBackend:
    return ClaudeCodeBackend(auth_mode=AuthMode(options["auth_mode"]), credentials=credentials)


SPEC = BackendSpec(
    name=NAME,
    display_name="Claude Code",
    models=MODELS,
    efforts=EFFORTS,
    factory=_create,
    options={
        "auth_mode": OptionSpec(
            allowed=tuple(mode.value for mode in AuthMode), default=AuthMode.SUBSCRIPTION.value
        )
    },
    credentials=tuple(name for name, _ in _CREDENTIALS.values()),
    web_tools=True,
)
