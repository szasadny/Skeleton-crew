from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skeleton_crew.agents.backends._shared import (
    AUTH_FILE_ENV,
    CHATGPT_HOSTS,
    OPENAI_KEY,
    REPORTED_ERROR,
    AuthMode,
    CliBackend,
    CliFiles,
    Credential,
    SessionReport,
    auth_mode_option,
    missing_outcome,
    read_stream,
    safe_session_id,
    session_outcome,
    stdin_command,
)
from skeleton_crew.agents.base import BackendSpec, SessionOutcome, SessionRequest

MODELS = (
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-6.1-terra",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)
# "ultra" is left out: it lets the model delegate to sub-agents on its own.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CREDENTIALS = {
    AuthMode.API_KEY: Credential(OPENAI_KEY.name, "CODEX_API_KEY"),
    AuthMode.SUBSCRIPTION: Credential("codex_auth_json", AUTH_FILE_ENV),
}
_EGRESS_DOMAINS = {AuthMode.API_KEY: (OPENAI_KEY.host,), AuthMode.SUBSCRIPTION: CHATGPT_HOSTS}
_AUTH_FILE = ".codex/auth.json"
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


@dataclass
class _Stream:
    session_id: str | None = None
    message: str | None = None
    turns: int = 0
    turn_failed: bool = False
    unrecovered_error: bool = False

    def add(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "thread.started":
            self.session_id = safe_session_id(event.get("thread_id"))
        elif kind == "item.completed" and (text := _agent_message(event)) is not None:
            self.message = text
        elif kind == "turn.completed":
            self.turns += 1
            # Codex also emits error events for transient failures it then retried.
            self.unrecovered_error = False
        elif kind == "turn.failed":
            self.turn_failed = True
        elif kind == "error":
            self.unrecovered_error = True


def _failure(stream: _Stream, exit_code: int) -> str | None:
    if stream.turn_failed:
        return "turn failed"
    if stream.unrecovered_error:
        return REPORTED_ERROR
    # A non-zero exit code is reported before a missing turn.
    if stream.turns == 0 and exit_code == 0:
        return "no completed turn"
    return None


class CodexBackend(CliBackend):
    name = "codex"
    display_name = "Codex"
    auto_loaded = ("AGENTS.md",)
    credentials = _CREDENTIALS

    def egress_domains(self) -> tuple[str, ...]:
        return _EGRESS_DOMAINS[self._auth_mode]

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
        files = CliFiles(
            req.prompt_path_in_container,
            rules_path=req.rules_path_in_container,
            auth_file=self._auth_file(_AUTH_FILE),
        )
        return stdin_command("codex", args, files)

    def parse(self, stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome:
        stream = read_stream(stdout_path, _Stream())
        if stream.session_id is None:
            return missing_outcome("session", exit_code, timed_out)
        report = SessionReport(
            session_id=stream.session_id,
            answer=stream.message,
            failure=_failure(stream, exit_code),
            turns=stream.turns,
        )
        return session_outcome(report, exit_code, timed_out)


SPEC = BackendSpec(
    name=CodexBackend.name,
    display_name=CodexBackend.display_name,
    models=MODELS,
    efforts=EFFORTS,
    factory=CodexBackend.create,
    options={"auth_mode": auth_mode_option(AuthMode.API_KEY)},
    credentials=CodexBackend.credential_names(),
    web_tools=True,
)
