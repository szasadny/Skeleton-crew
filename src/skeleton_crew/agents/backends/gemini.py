import json
from collections.abc import Mapping
from dataclasses import dataclass, field
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

NAME = "gemini"


class AuthMode(StrEnum):
    API_KEY = "api_key"
    SUBSCRIPTION = "subscription"


INTERACTIVE_TOOLS = ("ask_user", "enter_plan_mode", "exit_plan_mode")
WEB_TOOLS = ("google_web_search", "web_fetch")


def excluded_tools(*, web_tools: bool) -> list[str]:
    return list(INTERACTIVE_TOOLS if web_tools else (*INTERACTIVE_TOOLS, *WEB_TOOLS))


EGRESS_DOMAINS = {
    AuthMode.API_KEY: ("generativelanguage.googleapis.com",),
    AuthMode.SUBSCRIPTION: ("cloudcode-pa.googleapis.com", "oauth2.googleapis.com"),
}
AUTO_LOADED_DOCS = ("GEMINI.md",)
# Aliases follow the CLI's current default for that tier; full ids pin one.
MODELS = (
    "auto",
    "pro",
    "flash",
    "flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-pro",
)
EFFORTS = ("default",)
_CREDENTIALS = {
    AuthMode.API_KEY: ("gemini_api_key", "GEMINI_API_KEY"),
    AuthMode.SUBSCRIPTION: ("gemini_oauth_creds_json", AUTH_FILE_ENV),
}
_AUTH_TYPES = {AuthMode.API_KEY: "gemini-api-key", AuthMode.SUBSCRIPTION: "oauth-personal"}
OAUTH_FILE = ".gemini/oauth_creds.json"
SETTINGS_FILE = ".gemini/settings.json"
_BASE_ENV = {"GEMINI_TELEMETRY_ENABLED": "false", "NO_BROWSER": "true"}
_ERROR_REASONS = {"FatalTurnLimitedError": "max_turns", "FatalCancellationError": "cancelled"}
# Allowing only a name no server has keeps the target repo's MCP servers from loading.
_NO_MCP_SERVER = "skeleton-crew-no-mcp"


# The turn limit, tool exclusions and auth type exist only as settings, not as flags.
def _settings(req: SessionRequest, auth_mode: AuthMode) -> str:
    return json.dumps(
        {
            "model": {"maxSessionTurns": req.max_turns},
            "tools": {"exclude": excluded_tools(web_tools=req.agent.web_tools)},
            "security": {"auth": {"selectedType": _AUTH_TYPES[auth_mode]}},
            "privacy": {"usageStatisticsEnabled": False},
            "general": {"enableAutoUpdate": False, "enableAutoUpdateNotification": False},
        }
    )


@dataclass
class _Stream:
    session_id: str | None = None
    result: dict[str, Any] | None = None
    reported_error: bool = False
    turns: int = 0
    final_text: list[str] = field(default_factory=list)
    awaiting_model: bool = True

    def _model_turn(self) -> None:
        if self.awaiting_model:
            self.turns += 1
            self.awaiting_model = False

    def add(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "init" and self.session_id is None:
            self.session_id = safe_session_id(event.get("session_id"))
        elif kind == "message" and event.get("role") == "assistant":
            self._model_turn()
            content = event.get("content")
            if isinstance(content, str):
                self.final_text.append(content)
        elif kind == "tool_use":
            self._model_turn()
            self.final_text.clear()
        elif kind == "tool_result":
            self.awaiting_model = True
            self.final_text.clear()
        elif kind == "error" and event.get("severity") != "warning":
            self.reported_error = True
        elif kind == "result":
            self.result = event


def _read(stdout_path: Path) -> _Stream:
    stream = _Stream()
    for event in jsonl_events(stdout_path):
        stream.add(event)
    return stream


def _error_reason(result: dict[str, Any]) -> str:
    error = result.get("error")
    kind = error.get("type") if isinstance(error, dict) else None
    return _ERROR_REASONS.get(kind, "error") if isinstance(kind, str) else "error"


class GeminiBackend:
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
            return [f"Gemini CLI ({self._auth_mode.value} mode): {setting} is not set"]
        return []

    def env(self) -> dict[str, str]:
        var, value = self._credential()
        return {**_BASE_ENV, var: value} if value is not None else dict(_BASE_ENV)

    def command(self, req: SessionRequest) -> list[str]:
        args = [
            "--output-format",
            "stream-json",
            "--model",
            req.agent.model,
            "--approval-mode",
            "yolo",
            "--skip-trust",
            "--allowed-mcp-server-names",
            _NO_MCP_SERVER,
        ]
        if req.resume_session_id is not None:
            args += ["--resume", req.resume_session_id]
        subscription = self._auth_mode is AuthMode.SUBSCRIPTION
        return stdin_command(
            "gemini",
            args,
            prompt_path=req.prompt_path_in_container,
            rules_path=req.rules_path_in_container,
            auth_file=OAUTH_FILE if subscription else None,
            config_file=(SETTINGS_FILE, _settings(req, self._auth_mode)),
        )

    def parse(self, stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome:
        stream = _read(stdout_path)
        result = stream.result
        if result is None:
            missing = "timed out" if timed_out else f"no result in output (exit code {exit_code})"
            return failed_outcome(missing, timed_out=timed_out)
        structured = extract_json_text("".join(stream.final_text))
        error: str | None = None
        stopped_early: str | None = None
        if timed_out:
            error = "timed out"
        elif result.get("status") != "success":
            error = f"session ended with {_error_reason(result)}"
        elif stream.reported_error:
            error = "session reported an error"
        elif exit_code != 0:
            error = f"exit code {exit_code}"
        elif structured is None:
            error = "no structured output"
            stopped_early = "no_result" if stream.session_id is not None else None
        return SessionOutcome(
            session_id=stream.session_id,
            structured_output=structured,
            cost_usd=0.0,
            turns=stream.turns,
            ok=error is None,
            error=error,
            timed_out=timed_out,
            stopped_early=stopped_early,
        )


def _create(credentials: Mapping[str, SecretStr], options: Mapping[str, str]) -> GeminiBackend:
    return GeminiBackend(auth_mode=AuthMode(options["auth_mode"]), credentials=credentials)


SPEC = BackendSpec(
    name=NAME,
    display_name="Gemini CLI",
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
