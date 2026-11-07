from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from skeleton_crew.agents.backends._shared import (
    AUTH_FILE_ENV,
    jsonl_events,
    safe_session_id,
    secret_value,
    stdin_command,
)
from skeleton_crew.agents.base import (
    BackendSpec,
    OptionSpec,
    SessionOutcome,
    SessionRequest,
    failed_outcome,
)
from skeleton_crew.agents.output import extract_json_text

NAME = "codex"


class AuthMode(StrEnum):
    API_KEY = "api_key"
    SUBSCRIPTION = "subscription"


EGRESS_DOMAINS = {
    AuthMode.API_KEY: ("api.openai.com",),
    AuthMode.SUBSCRIPTION: ("chatgpt.com", "auth.openai.com"),
}
AUTO_LOADED_DOCS = ("AGENTS.md",)
MODELS = (
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-6.1-terra",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
)
# "ultra" is left out: it lets the model delegate to sub-agents on its own.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
AUTH_FILE = ".codex/auth.json"
_CREDENTIALS = {
    AuthMode.API_KEY: ("openai_api_key", "CODEX_API_KEY"),
    AuthMode.SUBSCRIPTION: ("codex_auth_json", AUTH_FILE_ENV),
}
_FIXED_CONFIG = ("analytics.enabled=false", "feedback.enabled=false")


def _config_args(*, effort: str, web_tools: bool) -> list[str]:
    # Codex enables live web search by default when the sandbox is bypassed.
    web_search = "live" if web_tools else "disabled"
    pairs = (f'model_reasoning_effort="{effort}"', f'web_search="{web_search}"', *_FIXED_CONFIG)
    return [arg for pair in pairs for arg in ("-c", pair)]


def _agent_message(event: dict[str, Any]) -> str | None:
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "agent_message":
        return None
    text = item.get("text")
    return text if isinstance(text, str) else None


class CodexBackend:
    name = NAME

    def __init__(self, *, auth_mode: AuthMode, credentials: Mapping[str, SecretStr]) -> None:
        self._auth_mode = auth_mode
        self._credentials = dict(credentials)

    def egress_domains(self) -> tuple[str, ...]:
        return EGRESS_DOMAINS[self._auth_mode]

    def auto_loaded_docs(self) -> tuple[str, ...]:
        return AUTO_LOADED_DOCS

    def _credential(self) -> tuple[str, str | None]:
        name, var = _CREDENTIALS[self._auth_mode]
        return var, secret_value(self._credentials, name)

    def credential_problems(self) -> list[str]:
        _, value = self._credential()
        if value is None:
            setting = f"SC_AGENT_CREDENTIALS__{_CREDENTIALS[self._auth_mode][0].upper()}"
            return [f"Codex ({self._auth_mode.value} mode): {setting} is not set"]
        return []

    def env(self) -> dict[str, str]:
        var, value = self._credential()
        return {var: value} if value is not None else {}

    def command(self, req: SessionRequest) -> list[str]:
        args = [
            "--json",
            "-m",
            req.agent.model,
            *_config_args(effort=req.agent.effort, web_tools=req.agent.web_tools),
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--ignore-user-config",
        ]
        if req.resume_session_id is not None:
            args = ["exec", "resume", *args, req.resume_session_id, "-"]
        else:
            args = ["exec", *args, "-"]
        subscription = self._auth_mode is AuthMode.SUBSCRIPTION
        return stdin_command(
            "codex",
            args,
            prompt_path=req.prompt_path_in_container,
            rules_path=req.rules_path_in_container,
            auth_file=AUTH_FILE if subscription else None,
        )

    def parse(self, stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome:
        session_id: str | None = None
        message: str | None = None
        turns = 0
        turn_failed = False
        unrecovered_error = False
        for event in jsonl_events(stdout_path):
            kind = event.get("type")
            if kind == "thread.started":
                session_id = safe_session_id(event.get("thread_id"))
            elif kind == "item.completed" and (text := _agent_message(event)) is not None:
                message = text
            elif kind == "turn.completed":
                turns += 1
                # Codex also emits error events for transient failures it then retried.
                unrecovered_error = False
            elif kind == "turn.failed":
                turn_failed = True
            elif kind == "error":
                unrecovered_error = True
        if session_id is None:
            missing = "timed out" if timed_out else f"no session in output (exit code {exit_code})"
            return failed_outcome(missing, timed_out=timed_out)
        structured = extract_json_text(message) if message is not None else None
        error: str | None = None
        stopped_early: str | None = None
        if timed_out:
            error = "timed out"
        elif turn_failed:
            error = "turn failed"
        elif unrecovered_error:
            error = "session reported an error"
        elif exit_code != 0:
            error = f"exit code {exit_code}"
        elif turns == 0:
            error = "no completed turn"
        elif structured is None:
            error = "no structured output"
            stopped_early = "no_result"
        return SessionOutcome(
            session_id=session_id,
            structured_output=structured,
            cost_usd=0.0,
            turns=turns,
            ok=error is None,
            error=error,
            timed_out=timed_out,
            stopped_early=stopped_early,
        )


def _create(credentials: Mapping[str, SecretStr], options: Mapping[str, str]) -> CodexBackend:
    return CodexBackend(auth_mode=AuthMode(options["auth_mode"]), credentials=credentials)


SPEC = BackendSpec(
    name=NAME,
    display_name="Codex",
    models=MODELS,
    efforts=EFFORTS,
    factory=_create,
    options={
        "auth_mode": OptionSpec(
            allowed=tuple(mode.value for mode in AuthMode), default=AuthMode.API_KEY.value
        )
    },
    credentials=tuple(name for name, _ in _CREDENTIALS.values()),
    web_tools=True,
)
