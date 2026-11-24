import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skeleton_crew.agents.backends._shared import (
    AUTH_FILE_ENV,
    ENDED_WITH,
    GEMINI_KEY,
    REPORTED_ERROR,
    AuthMode,
    CliBackend,
    CliFiles,
    Credential,
    SessionReport,
    auth_mode_option,
    blocked_tools,
    missing_outcome,
    read_stream,
    safe_session_id,
    session_outcome,
    stdin_command,
)
from skeleton_crew.agents.base import BackendSpec, SessionOutcome, SessionRequest

INTERACTIVE_TOOLS = ("ask_user", "enter_plan_mode", "exit_plan_mode")
WEB_TOOLS = ("google_web_search", "web_fetch")
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
SETTINGS_FILE = ".gemini/settings.json"
_CREDENTIALS = {
    AuthMode.API_KEY: GEMINI_KEY,
    AuthMode.SUBSCRIPTION: Credential("gemini_oauth_creds_json", AUTH_FILE_ENV),
}
_EGRESS_DOMAINS = {
    AuthMode.API_KEY: (GEMINI_KEY.host,),
    AuthMode.SUBSCRIPTION: ("cloudcode-pa.googleapis.com", "oauth2.googleapis.com"),
}
_AUTH_TYPES = {AuthMode.API_KEY: "gemini-api-key", AuthMode.SUBSCRIPTION: "oauth-personal"}
_OAUTH_FILE = ".gemini/oauth_creds.json"
_BASE_ENV = {"GEMINI_TELEMETRY_ENABLED": "false", "NO_BROWSER": "true"}
_ERROR_REASONS = {"FatalTurnLimitedError": "max_turns", "FatalCancellationError": "cancelled"}
# Allowing only a name no server has keeps the target repo's MCP servers from loading.
_NO_MCP_SERVER = "skeleton-crew-no-mcp"


# The turn limit, tool exclusions and auth type exist only as settings, not as flags.
def _settings(req: SessionRequest, auth_mode: AuthMode) -> str:
    excluded = blocked_tools(INTERACTIVE_TOOLS, WEB_TOOLS, web_tools=req.agent.web_tools)
    return json.dumps(
        {
            "model": {"maxSessionTurns": req.max_turns},
            "tools": {"exclude": list(excluded)},
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


def _error_reason(result: dict[str, Any]) -> str:
    error = result.get("error")
    kind = error.get("type") if isinstance(error, dict) else None
    return _ERROR_REASONS.get(kind, "error") if isinstance(kind, str) else "error"


def _failure(result: dict[str, Any], reported_error: bool) -> str | None:
    if result.get("status") != "success":
        return ENDED_WITH.format(_error_reason(result))
    return REPORTED_ERROR if reported_error else None


class GeminiBackend(CliBackend):
    name = "gemini"
    display_name = "Gemini CLI"
    auto_loaded = ("GEMINI.md",)
    base_env = _BASE_ENV
    credentials = _CREDENTIALS

    def egress_domains(self) -> tuple[str, ...]:
        return _EGRESS_DOMAINS[self._auth_mode]

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
        files = CliFiles(
            req.prompt_path_in_container,
            rules_path=req.rules_path_in_container,
            auth_file=self._auth_file(_OAUTH_FILE),
            config_file=(SETTINGS_FILE, _settings(req, self._auth_mode)),
        )
        return stdin_command("gemini", args, files)

    def parse(self, stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome:
        stream = read_stream(stdout_path, _Stream())
        if stream.result is None:
            return missing_outcome("result", exit_code, timed_out)
        report = SessionReport(
            session_id=stream.session_id,
            answer="".join(stream.final_text),
            failure=_failure(stream.result, stream.reported_error),
            turns=stream.turns,
        )
        return session_outcome(report, exit_code, timed_out)


SPEC = BackendSpec(
    name=GeminiBackend.name,
    display_name=GeminiBackend.display_name,
    models=MODELS,
    efforts=EFFORTS,
    factory=GeminiBackend.create,
    options={"auth_mode": auth_mode_option(AuthMode.API_KEY)},
    credentials=GeminiBackend.credential_names(),
)
