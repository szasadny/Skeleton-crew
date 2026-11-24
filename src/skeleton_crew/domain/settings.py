import re
from collections.abc import Mapping, Sequence
from datetime import time
from enum import StrEnum
from functools import partial
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator


class Weekday(StrEnum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"


class Role(StrEnum):
    SELECTOR = "selector"
    REFINER = "refiner"
    REVIEWER = "reviewer"
    WORKER = "worker"


COMMON_RULES = "common"
RULE_KEYS: tuple[str, ...] = (COMMON_RULES, *(role.value for role in Role))
MAX_RULES_CHARS = 20_000
MAX_NEW_ISSUES = 10


class InvalidRules(ValueError):
    pass


def check_rule_key(key: str) -> None:
    if key not in RULE_KEYS:
        raise ValueError(f"unknown rules key: {key}")


def check_rules_content(content: str) -> None:
    if not content.strip():
        raise InvalidRules("Rules cannot be empty.")
    if len(content) > MAX_RULES_CHARS:
        raise InvalidRules(f"Rules are limited to {MAX_RULES_CHARS} characters.")


# Neutral data only: agents/registry.py owns what is valid, and a test keeps these in sync.
DEFAULT_BACKEND = "claude_code"
DEFAULT_MODEL = "opus"
DEFAULT_EFFORT = "medium"
DEFAULT_REVIEWER_EFFORT = "high"
DEFAULT_CLASSIFIER = "jev"
DEFAULT_CLASSIFIER_MODEL = "jev-latest"

_DOMAIN_RE = re.compile(r"^(\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,99}$")
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_EFFORT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_OPTION_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
_MAX_OPTIONS = 20
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(\[bot\])?$")
_MAX_LABEL_CHARS = 50
_RUNNER_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_MAX_RUNNER_LABELS = 10
_NO_PROXY_RE = re.compile(r"^\.?[A-Za-z0-9:][A-Za-z0-9.:/_-]{0,252}$")
_MAX_NO_PROXY = 50
_ACTIONS_RESULTS_ACCOUNTS = 20
# Exact Actions storage hosts, so the runners get no wildcard into arbitrary Azure storage accounts.
_ACTIONS_RESULTS_HOSTS = tuple(
    f"productionresultssa{n}.blob.core.windows.net" for n in range(_ACTIONS_RESULTS_ACCOUNTS)
)
_SECONDS_PER_MINUTE = 60.0


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Schedule(_Frozen):
    weekdays: frozenset[Weekday] = frozenset()
    start_time: time = time(1, 0)
    window_hours: float = Field(default=6.0, gt=0, le=24)
    timezone: str = "Europe/Amsterdam"

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError, ValueError:
            raise ValueError(f"unknown timezone: {value}") from None
        return value


class Limits(_Frozen):
    issues_per_day: int = Field(default=10, ge=0, le=100)
    refines_per_day: int = Field(default=10, ge=0, le=50)
    conflict_fixes_per_day: int = Field(default=30, ge=0, le=50)
    max_parallel_workers: int = Field(default=2, ge=1, le=10)
    session_timeout_minutes: int = Field(default=60, ge=5, le=480)
    worker_timeout_hours: float = Field(default=5.0, gt=0, le=24)
    max_turns: int = Field(default=200, ge=10, le=2000)
    max_check_rounds: int = Field(default=3, ge=0, le=10)
    max_ci_rounds: int = Field(default=3, ge=0, le=10)
    max_policy_rounds: int = Field(default=3, ge=0, le=10)
    max_review_rounds: int = Field(default=3, ge=0, le=10)
    max_continues: int = Field(default=5, ge=0, le=20)
    max_new_issues_per_worker: int = Field(default=3, ge=0, le=MAX_NEW_ISSUES)
    max_budget_usd_per_session: float | None = Field(default=None, gt=0)
    max_diff_lines: int = Field(default=1500, ge=10, le=50_000)
    max_comment_chars: int = Field(default=1500, ge=200, le=20_000)
    max_code_comment_lines: int = Field(default=2, ge=1, le=20)
    command_timeout_minutes: int = Field(default=30, ge=1, le=240)
    ci_poll_seconds: int = Field(default=60, ge=10, le=900)
    ci_timeout_minutes: int = Field(default=60, ge=5, le=480)
    ci_grace_minutes: int = Field(default=10, ge=1, le=120)
    task_retention_days: int = Field(default=30, ge=1, le=365)

    @property
    def session_timeout_s(self) -> float:
        return self.session_timeout_minutes * _SECONDS_PER_MINUTE

    @property
    def command_timeout_s(self) -> float:
        return self.command_timeout_minutes * _SECONDS_PER_MINUTE

    @property
    def ci_timeout_s(self) -> float:
        return self.ci_timeout_minutes * _SECONDS_PER_MINUTE

    @property
    def ci_grace_s(self) -> float:
        return self.ci_grace_minutes * _SECONDS_PER_MINUTE


def _identifier(value: str, what: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{what} must be a lowercase identifier")
    return value


def _model(value: str) -> str:
    if not _MODEL_RE.fullmatch(value):
        raise ValueError("model name contains unsupported characters")
    return value


def _effort(value: str) -> str:
    if not _EFFORT_RE.fullmatch(value):
        raise ValueError("effort contains unsupported characters")
    return value


_BackendName = Annotated[str, AfterValidator(partial(_identifier, what="backend"))]
_ProviderName = Annotated[str, AfterValidator(partial(_identifier, what="provider"))]
_ModelName = Annotated[str, AfterValidator(_model)]
_EffortName = Annotated[str, AfterValidator(_effort)]


class RoleAgent(_Frozen):
    backend: _BackendName = DEFAULT_BACKEND
    model: _ModelName = DEFAULT_MODEL
    effort: _EffortName = DEFAULT_EFFORT
    web_tools: bool = False


def _default_agents() -> dict[Role, RoleAgent]:
    return {
        Role.SELECTOR: RoleAgent(),
        Role.REFINER: RoleAgent(),
        Role.REVIEWER: RoleAgent(effort=DEFAULT_REVIEWER_EFFORT),
        Role.WORKER: RoleAgent(),
    }


class LabelNames(_Frozen):
    ready: str = "skeleton"
    refine: str = "skeleton refine"
    hitl: str = "skeleton HITL"

    @model_validator(mode="after")
    def _distinct_and_non_empty(self) -> LabelNames:
        names = [self.ready, self.refine, self.hitl]
        if any(not n.strip() or len(n) > _MAX_LABEL_CHARS for n in names):
            raise ValueError(f"label names must be 1-{_MAX_LABEL_CHARS} non-blank characters")
        if len({n.casefold() for n in names}) != len(names):
            raise ValueError("label names must be distinct")
        return self


class RepoPolicy(_Frozen):
    base_branch: str = "main"
    protected_paths: tuple[str, ...] = (".github/**", ".gitmodules")
    setup_command: str | None = Field(default=None, max_length=500)
    check_command: str | None = Field(default=None, max_length=500)
    require_ci: bool = True
    allow_unprotected_base_branch: bool = False
    trusted_users: tuple[str, ...] = ()

    @field_validator("trusted_users")
    @classmethod
    def _valid_logins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for login in value:
            if not LOGIN_RE.fullmatch(login):
                raise ValueError(f"invalid GitHub login: {login}")
        return value

    @field_validator("protected_paths")
    @classmethod
    def _valid_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            if not pattern or pattern.startswith("/") or ".." in pattern.split("/"):
                raise ValueError(f"invalid protected path pattern: {pattern}")
        return value


def check_domains(value: tuple[str, ...]) -> tuple[str, ...]:
    for domain in value:
        if not _DOMAIN_RE.fullmatch(domain):
            raise ValueError(f"invalid domain: {domain}")
    return value


_Domains = Annotated[tuple[str, ...], AfterValidator(check_domains)]
_Cpus = Annotated[float, Field(gt=0, le=64)]
_MemoryGb = Annotated[float, Field(gt=0, le=256)]
_Pids = Annotated[int, Field(ge=128, le=65_536)]


_PACKAGE_HOSTS = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "registry-1.docker.io",
    "auth.docker.io",
    "production.cloudfront.docker.com",
)


class RuntimeLimits(_Frozen):
    egress_allowlist: _Domains = _PACKAGE_HOSTS
    cpus: _Cpus = 2.0
    memory_gb: _MemoryGb = 4.0
    pids: _Pids = 2048


class SelfHostedCi(_Frozen):
    enabled: bool = False
    runners: int = Field(default=2, ge=1, le=10)
    labels: tuple[str, ...] = ("skeleton-crew",)
    egress_allowlist: _Domains = (
        "github.com",
        "api.github.com",
        "codeload.github.com",
        ".actions.githubusercontent.com",
        "objects.githubusercontent.com",
        "raw.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "ghcr.io",
        "pkg-containers.githubusercontent.com",
        *_ACTIONS_RESULTS_HOSTS,
        *_PACKAGE_HOSTS,
    )
    no_proxy: tuple[str, ...] = ()
    cpus: _Cpus = 2.0
    memory_gb: _MemoryGb = 4.0
    pids: _Pids = 4096

    @field_validator("no_proxy")
    @classmethod
    def _valid_no_proxy(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > _MAX_NO_PROXY:
            raise ValueError(f"at most {_MAX_NO_PROXY} no-proxy hosts")
        for host in value:
            if not _NO_PROXY_RE.fullmatch(host):
                raise ValueError(f"invalid no-proxy host: {host}")
        return value

    @field_validator("labels")
    @classmethod
    def _valid_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) > _MAX_RUNNER_LABELS:
            raise ValueError(f"give 1-{_MAX_RUNNER_LABELS} runner labels")
        for runner_label in value:
            if not _RUNNER_LABEL_RE.fullmatch(runner_label):
                raise ValueError(f"invalid runner label: {runner_label}")
        return value


CLASSIFIER_USE_CASES = ("injection_screen", "prescreen", "effort_routing")
ISSUE_SIZES = ("small", "medium", "large")


def routed_effort_field(size: str) -> str:
    return f"effort_{size}"


class ClassifierSettings(_Frozen):
    provider: _ProviderName = DEFAULT_CLASSIFIER
    model: _ModelName = DEFAULT_CLASSIFIER_MODEL
    injection_screen: bool = False
    prescreen: bool = False
    effort_routing: bool = False
    effort_small: _EffortName = "low"
    effort_medium: _EffortName = "medium"
    effort_large: _EffortName = "high"

    @property
    def enabled(self) -> bool:
        return any(getattr(self, use_case) for use_case in CLASSIFIER_USE_CASES)

    @property
    def routed_efforts(self) -> dict[str, str]:
        return {size: getattr(self, routed_effort_field(size)) for size in ISSUE_SIZES}


class AppSettings(_Frozen):
    schedule: Schedule = Schedule()
    limits: Limits = Limits()
    agents: dict[Role, RoleAgent] = Field(default_factory=_default_agents)
    backend_options: dict[str, dict[str, str]] = Field(default_factory=dict)
    labels: LabelNames = LabelNames()
    repo: RepoPolicy = RepoPolicy()
    runtime: RuntimeLimits = RuntimeLimits()
    ci: SelfHostedCi = SelfHostedCi()
    classifier: ClassifierSettings = ClassifierSettings()
    dry_run: bool = True
    kill_switch: bool = False

    @field_validator("agents")
    @classmethod
    def _all_roles(cls, value: dict[Role, RoleAgent]) -> dict[Role, RoleAgent]:
        missing = set(Role) - set(value)
        if missing:
            raise ValueError(f"missing agent config for: {sorted(missing)}")
        return value

    @field_validator("backend_options")
    @classmethod
    def _valid_options(cls, value: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        if len(value) > _MAX_OPTIONS:
            raise ValueError("too many backends in backend_options")
        for backend, options in value.items():
            _identifier(backend, "backend")
            if len(options) > _MAX_OPTIONS:
                raise ValueError(f"too many options for {backend}")
            for key, option in options.items():
                _identifier(key, "option name")
                if not _OPTION_VALUE_RE.fullmatch(option):
                    raise ValueError(f"{backend}.{key} contains unsupported characters")
        return value


def problem_lines(problems: Mapping[str, Sequence[str]]) -> list[str]:
    return [f"{field}: {message}" for field, messages in problems.items() for message in messages]


def with_updates(settings: AppSettings, updates: Mapping[str, object]) -> AppSettings:
    data: dict[str, Any] = settings.model_dump(mode="json")
    for dotted, value in updates.items():
        *parents, leaf = dotted.split(".")
        target = data
        for part in parents:
            target = target.setdefault(part, {})
        target[leaf] = value
    return AppSettings.model_validate(data)
