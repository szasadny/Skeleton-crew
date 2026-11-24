import json
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol, Self

from pydantic import SecretStr

from skeleton_crew.agents.base import OptionSpec, SessionOutcome, SessionRequest, failed_outcome
from skeleton_crew.core.config import credential_setting, secret_value

AUTH_FILE_ENV = "SC_AUTH_FILE"
TIMED_OUT = "timed out"
REPORTED_ERROR = "session reported an error"
NO_STRUCTURED_OUTPUT = "no structured output"
EXIT_CODE = "exit code {}"
ENDED_WITH = "session ended with {}"

_BINARY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_REASON_RE = re.compile(r"^[a-z_]{1,40}$")
# Ids are later passed back to the CLI, so they must never look like an option.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_FENCED_JSON_RE = re.compile(r"\A```json[ \t]*\n(?P<body>.*?)\n```\Z", re.DOTALL)
# Never overwrite: the CLI may have refreshed the login during an earlier session.
_AUTH_SCRIPT = (
    'auth="$HOME/$1"; shift; mkdir -p "$(dirname "$auth")";'
    f' [ -e "$auth" ] || printf "%s" "${AUTH_FILE_ENV}" > "$auth"; unset {AUTH_FILE_ENV};'
)
# Always rewritten, after removing whatever the agent may have left there (e.g. a symlink).
_CONFIG_SCRIPT = (
    'config="$HOME/$1"; mkdir -p "$(dirname "$config")" && rm -rf "$config"'
    ' && printf "%s" "$2" > "$config" || exit 1; shift 2;'
)


class AuthMode(StrEnum):
    API_KEY = "api_key"
    SUBSCRIPTION = "subscription"


@dataclass(frozen=True)
class Credential:
    name: str
    env_var: str


@dataclass(frozen=True)
class ApiKey(Credential):
    host: str


ANTHROPIC_KEY = ApiKey("anthropic_api_key", "ANTHROPIC_API_KEY", "api.anthropic.com")
OPENAI_KEY = ApiKey("openai_api_key", "OPENAI_API_KEY", "api.openai.com")
GEMINI_KEY = ApiKey("gemini_api_key", "GEMINI_API_KEY", "generativelanguage.googleapis.com")
CHATGPT_HOSTS = ("chatgpt.com", "auth.openai.com")


def auth_mode_option(default: AuthMode) -> OptionSpec:
    others = tuple(mode.value for mode in AuthMode if mode is not default)
    return OptionSpec(allowed=(default.value, *others), default=default.value)


def blocked_tools(
    interactive: tuple[str, ...], web: tuple[str, ...], *, web_tools: bool
) -> tuple[str, ...]:
    return interactive if web_tools else (*interactive, *web)


class CliBackend(ABC):
    name: str
    display_name: ClassVar[str]
    auto_loaded: ClassVar[tuple[str, ...]]
    base_env: ClassVar[Mapping[str, str]] = {}
    credentials: ClassVar[Mapping[AuthMode, Credential]]

    def __init__(self, *, auth_mode: AuthMode, credentials: Mapping[str, SecretStr]) -> None:
        self._auth_mode = auth_mode
        self._credentials = dict(credentials)

    @classmethod
    def create(cls, credentials: Mapping[str, SecretStr], options: Mapping[str, str]) -> Self:
        return cls(auth_mode=AuthMode(options["auth_mode"]), credentials=credentials)

    @classmethod
    def credential_names(cls) -> tuple[str, ...]:
        return tuple(credential.name for credential in cls.credentials.values())

    @abstractmethod
    def egress_domains(self) -> tuple[str, ...]: ...

    @abstractmethod
    def command(self, req: SessionRequest) -> list[str]: ...

    @abstractmethod
    def parse(self, stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome: ...

    def auto_loaded_docs(self) -> tuple[str, ...]:
        return self.auto_loaded

    def _credential(self) -> Credential:
        return self.credentials[self._auth_mode]

    def _mode(self) -> str:
        return f"{self._auth_mode.value} mode"

    def _secret(self) -> str | None:
        return secret_value(self._credentials.get(self._credential().name))

    def credential_problems(self) -> list[str]:
        if self._secret() is None:
            setting = credential_setting(self._credential().name)
            return [f"{self.display_name} ({self._mode()}): {setting} is not set"]
        return []

    def env(self) -> dict[str, str]:
        value = self._secret()
        if value is None:
            return dict(self.base_env)
        return {**self.base_env, self._credential().env_var: value}

    def _auth_file(self, path: str) -> str | None:
        return path if self._auth_mode is AuthMode.SUBSCRIPTION else None


def number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def safe_session_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _SESSION_ID_RE.match(value) else None


def safe_reason(value: Any) -> str | None:
    return value if isinstance(value, str) and _REASON_RE.match(value) else None


def json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError, RecursionError:
        return None
    return value if isinstance(value, dict) else None


def extract_json_text(answer: str | None) -> str | None:
    if answer is None:
        return None
    text = answer.strip()
    if not text.startswith("{"):
        match = _FENCED_JSON_RE.fullmatch(text)
        if match is None or "```" in match["body"]:
            return None
        text = match["body"]
    value = json_object(text)
    return json.dumps(value) if value is not None else None


def _lines(path: Path) -> Iterator[str]:
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            yield from stream
    except FileNotFoundError:
        return


def jsonl_events(stdout_path: Path) -> Iterator[dict[str, Any]]:
    for line in _lines(stdout_path):
        if (event := json_object(line)) is not None:
            yield event


class EventStream(Protocol):
    def add(self, event: dict[str, Any]) -> None: ...


def read_stream[S: EventStream](stdout_path: Path, stream: S) -> S:
    for event in jsonl_events(stdout_path):
        stream.add(event)
    return stream


@dataclass(frozen=True)
class SessionReport:
    session_id: str | None
    answer: str | None
    failure: str | None = None
    cost: float = 0.0
    turns: int = 0
    stop_reason: object = None
    limited: bool = False
    stops_without_session: bool = False


def missing_outcome(what: str, exit_code: int, timed_out: bool) -> SessionOutcome:
    error = TIMED_OUT if timed_out else f"no {what} in output (exit code {exit_code})"
    return failed_outcome(error, timed_out=timed_out)


def _error(failure: str | None, exit_code: int, timed_out: bool) -> str | None:
    if timed_out:
        return TIMED_OUT
    if failure is not None:
        return failure
    return EXIT_CODE.format(exit_code) if exit_code != 0 else None


def session_outcome(report: SessionReport, exit_code: int, timed_out: bool) -> SessionOutcome:
    structured = extract_json_text(report.answer)
    error = _error(report.failure, exit_code, timed_out)
    stopped_early: str | None = None
    if error is None and structured is None:
        error = NO_STRUCTURED_OUTPUT
        if report.session_id is not None or report.stops_without_session:
            stopped_early = safe_reason(report.stop_reason) or "no_result"
    return SessionOutcome(
        session_id=report.session_id,
        structured_output=structured,
        cost_usd=report.cost,
        turns=report.turns,
        ok=error is None,
        error=error,
        timed_out=timed_out,
        stopped_early=stopped_early,
        # A limit signal counts only on a failure: a rejected window can still succeed on overage.
        usage_limited=report.limited and error is not None,
    )


@dataclass(frozen=True)
class CliFiles:
    prompt_path: str
    rules_path: str | None = None
    auth_file: str | None = None
    config_file: tuple[str, str] | None = None


def _home_files(files: CliFiles) -> tuple[list[str], list[str]]:
    script: list[str] = []
    values: list[str] = []
    if files.auth_file is not None:
        script.append(_AUTH_SCRIPT)
        values.append(files.auth_file)
    if files.config_file is not None:
        script.append(_CONFIG_SCRIPT)
        values.extend(files.config_file)
    return (["umask 077;", *script] if script else script), values


def stdin_command(binary: str, args: Sequence[str], files: CliFiles) -> list[str]:
    if not _BINARY_RE.fullmatch(binary):
        raise ValueError(f"invalid binary name: {binary}")
    # Paths travel as positional arguments, never inside the script, so they cannot inject.
    script, values = ['prompt="$1"; shift;'], [files.prompt_path]
    if files.rules_path is not None:
        script.append('rules="$1"; shift;')
        values.append(files.rules_path)
    home_script, home_values = _home_files(files)
    script += home_script
    values += home_values
    if files.rules_path is None:
        script.append(f'exec {binary} "$@" < "$prompt"')
    else:
        script.append(f'{{ cat "$rules"; printf "\\n\\n"; cat "$prompt"; }} | exec {binary} "$@"')
    # On stdin, because Linux caps a single argument at 128 KiB and prompts can be larger.
    return ["sh", "-c", " ".join(script), "sh", *values, *args]
