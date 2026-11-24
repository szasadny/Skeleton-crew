import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from skeleton_crew.agents.backends._shared import (
    ANTHROPIC_KEY,
    AUTH_FILE_ENV,
    CHATGPT_HOSTS,
    GEMINI_KEY,
    OPENAI_KEY,
    REPORTED_ERROR,
    ApiKey,
    AuthMode,
    CliBackend,
    CliFiles,
    Credential,
    SessionReport,
    auth_mode_option,
    json_object,
    missing_outcome,
    number,
    read_stream,
    safe_session_id,
    session_outcome,
    stdin_command,
)
from skeleton_crew.agents.base import BackendSpec, OptionSpec, SessionOutcome, SessionRequest

CONFIG_ENV = "OPENCODE_CONFIG_CONTENT"


class Provider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    GOOGLE = "google"


_PROVIDERS = {
    Provider.ANTHROPIC: ANTHROPIC_KEY,
    Provider.OPENAI: OPENAI_KEY,
    Provider.OPENROUTER: ApiKey("openrouter_api_key", "OPENROUTER_API_KEY", "openrouter.ai"),
    Provider.GOOGLE: replace(GEMINI_KEY, env_var="GOOGLE_GENERATIVE_AI_API_KEY"),
}
# A ChatGPT sign-in, which OpenCode supports only for the OpenAI provider.
_SUBSCRIPTION_PROVIDER = Provider.OPENAI
_SUBSCRIPTION = Credential("opencode_auth_json", AUTH_FILE_ENV)
_AUTH_FILE = ".local/share/opencode/auth.json"
# Ids must exist in the model snapshot bundled with the pinned CLI, as catalog fetches are off.
MODELS = (
    "anthropic/claude-opus-5-5",
    "anthropic/claude-sonnet-5-5",
    "anthropic/claude-haiku-5-5",
    "anthropic/claude-fable-5-1",
    "openai/gpt-6.1-sol",
    "openai/gpt-6.1-terra",
    "openai/gpt-6-sol",
    "openai/gpt-6-luna",
    "openrouter/anthropic/claude-opus-5.5",
    "openrouter/anthropic/claude-sonnet-5.5",
    "openrouter/openai/gpt-6.1-sol",
    "openrouter/google/gemini-4-argon",
    "google/gemini-4-argon",
    "google/gemini-3.8-flash",
    "google/gemini-pro-latest",
)
# Passed as --variant; OpenCode ignores a variant the model does not define.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
_BASE_ENV = {
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
}
# OpenAI billing and spend-limit codes; per its docs, retrying never restores access.
_QUOTA_CODES = frozenset(
    {
        "insufficient_quota",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    }
)
_PAYMENT_REQUIRED = 402
# OpenRouter's one 402 that clears by itself after Retry-After.
_TRANSIENT_LIMIT_SOURCE = "openrouter_in_flight_budget"


def provider_models(model: str, options: Mapping[str, str]) -> bool:
    return model.startswith(f"{options['provider']}/")


def _permissions(*, web_tools: bool) -> dict[str, str]:
    web = "allow" if web_tools else "deny"
    return {"*": "allow", "question": "deny", "webfetch": web, "websearch": web}


def _session_config(req: SessionRequest) -> str:
    permission = _permissions(web_tools=req.agent.web_tools)
    config = {
        "autoupdate": False,
        "share": "disabled",
        "mcp": {},
        "plugin": [],
        "lsp": False,
        "formatter": False,
        "instructions": [req.rules_path_in_container],
        "permission": permission,
        "default_agent": "build",
        # Title generation is an extra model call whose cost the event stream never reports.
        "agent": {
            "build": {"steps": req.max_turns, "permission": permission},
            "title": {"disable": True},
        },
    }
    return json.dumps(config, separators=(",", ":"))


def _provider_error(body: Any) -> dict[str, Any]:
    parsed = json_object(body) if isinstance(body, str) else None
    error = parsed.get("error") if parsed is not None else None
    return error if isinstance(error, dict) else {}


def _is_limit_signal(error: Any) -> bool:
    if not isinstance(error, dict) or error.get("name") != "APIError":
        return False
    data = error.get("data")
    if not isinstance(data, dict):
        return False
    provider_error = _provider_error(data.get("responseBody"))
    codes = (provider_error.get("code"), provider_error.get("type"))
    if any(isinstance(code, str) and code in _QUOTA_CODES for code in codes):
        return True
    metadata = provider_error.get("metadata")
    transient = (
        isinstance(metadata, dict) and metadata.get("limit_source") == _TRANSIENT_LIMIT_SOURCE
    )
    return data.get("statusCode") == _PAYMENT_REQUIRED and not transient


@dataclass
class _Stream:
    session_id: str | None = None
    final_text: str | None = None
    finish_reason: object = None
    cost: float = 0.0
    turns: int = 0
    seen: bool = False
    errored: bool = False
    limited: bool = False

    def add(self, event: dict[str, Any]) -> None:
        handler = _HANDLERS.get(event.get("type"))
        if handler is None:
            return
        self.seen = True
        if (session_id := safe_session_id(event.get("sessionID"))) is not None:
            self.session_id = session_id
        part = event.get("part")
        handler(self, event, part if isinstance(part, dict) else {})

    def step_start(self, event: dict[str, Any], part: dict[str, Any]) -> None:
        self.turns += 1
        self.final_text = None

    def text(self, event: dict[str, Any], part: dict[str, Any]) -> None:
        if isinstance(part.get("text"), str):
            self.final_text = part["text"]

    def step_finish(self, event: dict[str, Any], part: dict[str, Any]) -> None:
        self.cost += max(number(part.get("cost")), 0.0)
        self.finish_reason = part.get("reason")

    def tool_use(self, event: dict[str, Any], part: dict[str, Any]) -> None:
        return

    def error(self, event: dict[str, Any], part: dict[str, Any]) -> None:
        self.errored = True
        self.limited = self.limited or _is_limit_signal(event.get("error"))


_HANDLERS: Mapping[object, Callable[[_Stream, dict[str, Any], dict[str, Any]], None]] = {
    "step_start": _Stream.step_start,
    "text": _Stream.text,
    "step_finish": _Stream.step_finish,
    "tool_use": _Stream.tool_use,
    "error": _Stream.error,
}


class OpenCodeBackend(CliBackend):
    name = "opencode"
    display_name = "OpenCode"
    # With CLAUDE.md compatibility off, OpenCode loads the root AGENTS.md (CONTEXT.md if absent).
    auto_loaded = ("AGENTS.md",)
    base_env = _BASE_ENV

    def __init__(
        self,
        *,
        provider: Provider,
        credentials: Mapping[str, SecretStr],
        auth_mode: AuthMode = AuthMode.API_KEY,
    ) -> None:
        super().__init__(auth_mode=auth_mode, credentials=credentials)
        self._provider = provider

    def egress_domains(self) -> tuple[str, ...]:
        if self._auth_mode is AuthMode.SUBSCRIPTION:
            return CHATGPT_HOSTS
        return (_PROVIDERS[self._provider].host,)

    def _credential(self) -> Credential:
        if self._auth_mode is AuthMode.SUBSCRIPTION:
            return _SUBSCRIPTION
        return _PROVIDERS[self._provider]

    def _mode(self) -> str:
        return f"{self._provider.value} provider, {self._auth_mode.value} mode"

    def credential_problems(self) -> list[str]:
        subscription = self._auth_mode is AuthMode.SUBSCRIPTION
        if subscription and self._provider is not _SUBSCRIPTION_PROVIDER:
            problem = "subscription mode needs the openai provider"
            return [f"{self.display_name} ({self._mode()}): {problem}"]
        return super().credential_problems()

    def command(self, req: SessionRequest) -> list[str]:
        # OpenCode has no config flag, so the per-request config reaches it as env through env(1).
        args = [
            f"{CONFIG_ENV}={_session_config(req)}",
            "opencode",
            "run",
            "--format",
            "json",
            "-m",
            req.agent.model,
            "--variant",
            req.agent.effort,
        ]
        if req.resume_session_id is not None:
            args += ["--session", req.resume_session_id]
        files = CliFiles(req.prompt_path_in_container, auth_file=self._auth_file(_AUTH_FILE))
        return stdin_command("env", args, files)

    def parse(self, stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome:
        stream = read_stream(stdout_path, _Stream())
        if not stream.seen:
            return missing_outcome("events", exit_code, timed_out)
        report = SessionReport(
            session_id=stream.session_id,
            answer=stream.final_text,
            failure=REPORTED_ERROR if stream.errored else None,
            cost=stream.cost,
            turns=stream.turns,
            stop_reason=stream.finish_reason,
            limited=stream.limited,
        )
        return session_outcome(report, exit_code, timed_out)


def _create(credentials: Mapping[str, SecretStr], options: Mapping[str, str]) -> OpenCodeBackend:
    return OpenCodeBackend(
        provider=Provider(options["provider"]),
        credentials=credentials,
        auth_mode=AuthMode(options["auth_mode"]),
    )


SPEC = BackendSpec(
    name=OpenCodeBackend.name,
    display_name=OpenCodeBackend.display_name,
    models=MODELS,
    efforts=EFFORTS,
    factory=_create,
    options={
        "provider": OptionSpec(
            allowed=tuple(provider.value for provider in Provider),
            default=Provider.ANTHROPIC.value,
        ),
        "auth_mode": auth_mode_option(AuthMode.API_KEY),
    },
    credentials=(*(key.name for key in _PROVIDERS.values()), _SUBSCRIPTION.name),
    web_tools=True,
    model_filter=provider_models,
)
