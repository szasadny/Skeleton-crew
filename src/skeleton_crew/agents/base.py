from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from skeleton_crew.domain.settings import Role, RoleAgent


@dataclass(frozen=True)
class ExecOutcome:
    exit_code: int
    timed_out: bool
    stderr_tail: str


class Executor(Protocol):
    # Enforces timeout_s itself (SIGINT, then a hard kill) and reports it as timed_out.
    async def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        timeout_s: float,
        stdout_path: Path,
    ) -> ExecOutcome: ...


@dataclass(frozen=True)
class SessionRequest:
    role: Role
    prompt: str
    agent: RoleAgent
    max_turns: int
    timeout_s: float
    json_schema: str
    rules_path_in_container: str
    prompt_path_in_container: str
    schema_path_in_container: str
    cwd: str
    resume_session_id: str | None = None
    max_budget_usd: float | None = None


@dataclass(frozen=True)
class SessionOutcome:
    session_id: str | None
    structured_output: str | None
    cost_usd: float
    turns: int
    ok: bool
    error: str | None
    timed_out: bool
    # Short reason code when the session ended cleanly without a result, e.g. a hook stopped it.
    stopped_early: str | None = None


class AgentBackend(Protocol):
    name: str

    def egress_domains(self) -> tuple[str, ...]: ...

    def auto_loaded_docs(self) -> tuple[str, ...]: ...

    def credential_problems(self) -> list[str]: ...

    def command(self, req: SessionRequest) -> list[str]: ...

    def env(self) -> dict[str, str]: ...

    def parse(self, stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome: ...


@dataclass(frozen=True)
class OptionSpec:
    allowed: tuple[str, ...]
    default: str


type BackendFactory = Callable[[Mapping[str, SecretStr], Mapping[str, str]], AgentBackend]
# Narrows the models to those valid for the resolved options, e.g. a provider prefix.
type ModelFilter = Callable[[str, Mapping[str, str]], bool]


@dataclass(frozen=True)
class BackendSpec:
    name: str
    display_name: str
    models: tuple[str, ...]
    efforts: tuple[str, ...]
    factory: BackendFactory
    options: Mapping[str, OptionSpec] = field(default_factory=dict)
    credentials: tuple[str, ...] = ()
    web_tools: bool = False
    model_filter: ModelFilter | None = None


def failed_outcome(error: str, *, timed_out: bool = False) -> SessionOutcome:
    return SessionOutcome(
        session_id=None,
        structured_output=None,
        cost_usd=0.0,
        turns=0,
        ok=False,
        error=error,
        timed_out=timed_out,
    )
