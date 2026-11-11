import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from skeleton_crew.agents.backends._shared import (
    jsonl_events,
    number,
    safe_reason,
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

NAME = "opencode"
CONFIG_ENV = "OPENCODE_CONFIG_CONTENT"


class Provider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    GOOGLE = "google"


@dataclass(frozen=True)
class _ProviderAccess:
    credential: str
    env_var: str
    domain: str


_PROVIDERS = {
    Provider.ANTHROPIC: _ProviderAccess(
        "anthropic_api_key", "ANTHROPIC_API_KEY", "api.anthropic.com"
    ),
    Provider.OPENAI: _ProviderAccess("openai_api_key", "OPENAI_API_KEY", "api.openai.com"),
    Provider.OPENROUTER: _ProviderAccess(
        "openrouter_api_key", "OPENROUTER_API_KEY", "openrouter.ai"
    ),
    Provider.GOOGLE: _ProviderAccess(
        "gemini_api_key", "GOOGLE_GENERATIVE_AI_API_KEY", "generativelanguage.googleapis.com"
    ),
}
# With CLAUDE.md compatibility off, OpenCode loads the root AGENTS.md (CONTEXT.md only without it).
AUTO_LOADED_DOCS = ("AGENTS.md",)
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


def session_config(req: SessionRequest) -> str:
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
    if not isinstance(body, str):
        return {}
    try:
        parsed = json.loads(body)
    except ValueError, RecursionError:
        return {}
    error = parsed.get("error") if isinstance(parsed, dict) else None
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


class OpenCodeBackend:
    name = NAME

    def __init__(self, *, provider: Provider, credentials: Mapping[str, SecretStr]) -> None:
        self._provider = provider
        self._credentials = dict(credentials)

    def egress_domains(self) -> tuple[str, ...]:
        return (_PROVIDERS[self._provider].domain,)

    def auto_loaded_docs(self) -> tuple[str, ...]:
        return AUTO_LOADED_DOCS

    def _credential(self) -> tuple[str, str | None]:
        access = _PROVIDERS[self._provider]
        return access.env_var, secret_value(self._credentials, access.credential)

    def credential_problems(self) -> list[str]:
        _, value = self._credential()
        if value is None:
            setting = f"SC_AGENT_CREDENTIALS__{_PROVIDERS[self._provider].credential.upper()}"
            return [f"OpenCode ({self._provider.value} provider): {setting} is not set"]
        return []

    def env(self) -> dict[str, str]:
        var, value = self._credential()
        return {**_BASE_ENV, var: value} if value is not None else dict(_BASE_ENV)

    def command(self, req: SessionRequest) -> list[str]:
        # OpenCode has no config flag, so the per-request config reaches it as env through env(1).
        args = [
            f"{CONFIG_ENV}={session_config(req)}",
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
        return stdin_command("env", args, prompt_path=req.prompt_path_in_container)

    def parse(self, stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome:
        session_id: str | None = None
        final_text: str | None = None
        finish_reason: object = None
        cost = 0.0
        turns = 0
        seen = False
        errored = False
        limited = False
        for event in jsonl_events(stdout_path):
            kind = event.get("type")
            if kind not in ("step_start", "text", "step_finish", "tool_use", "error"):
                continue
            seen = True
            if (sid := safe_session_id(event.get("sessionID"))) is not None:
                session_id = sid
            part = event.get("part")
            part = part if isinstance(part, dict) else {}
            if kind == "step_start":
                turns += 1
                final_text = None
            elif kind == "text" and isinstance(part.get("text"), str):
                final_text = part["text"]
            elif kind == "step_finish":
                cost += max(number(part.get("cost"), 0.0), 0.0)
                finish_reason = part.get("reason")
            elif kind == "error":
                errored = True
                limited = limited or _is_limit_signal(event.get("error"))
        if not seen:
            missing = "timed out" if timed_out else f"no events in output (exit code {exit_code})"
            return failed_outcome(missing, timed_out=timed_out)
        structured = extract_json_text(final_text) if final_text is not None else None
        error: str | None = None
        stopped_early: str | None = None
        if timed_out:
            error = "timed out"
        elif errored:
            error = "session reported an error"
        elif exit_code != 0:
            error = f"exit code {exit_code}"
        elif structured is None:
            error = "no structured output"
            if session_id is not None:
                stopped_early = safe_reason(finish_reason) or "no_result"
        return SessionOutcome(
            session_id=session_id,
            structured_output=structured,
            cost_usd=cost,
            turns=turns,
            ok=error is None,
            error=error,
            timed_out=timed_out,
            stopped_early=stopped_early,
            usage_limited=limited and error is not None,
        )


def _create(credentials: Mapping[str, SecretStr], options: Mapping[str, str]) -> OpenCodeBackend:
    return OpenCodeBackend(provider=Provider(options["provider"]), credentials=credentials)


SPEC = BackendSpec(
    name=NAME,
    display_name="OpenCode",
    models=MODELS,
    efforts=EFFORTS,
    factory=_create,
    options={
        "provider": OptionSpec(
            allowed=tuple(provider.value for provider in Provider),
            default=Provider.ANTHROPIC.value,
        )
    },
    credentials=tuple(access.credential for access in _PROVIDERS.values()),
    web_tools=True,
    model_filter=provider_models,
)
