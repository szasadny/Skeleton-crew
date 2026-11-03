import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from skeleton_crew.domain.settings import AgentBackendName, Role, RoleAgent

TIMEOUT_EXIT_CODE = 124
KILL_AFTER_S = 30


@dataclass(frozen=True)
class ExecOutcome:
    exit_code: int
    timed_out: bool
    stderr_tail: str


class Executor(Protocol):
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
    name: AgentBackendName

    def egress_domains(self) -> tuple[str, ...]: ...

    def credential_problems(self) -> list[str]: ...

    def command(self, req: SessionRequest) -> list[str]: ...

    def env(self) -> dict[str, str]: ...

    def parse(self, stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome: ...


def timeout_argv(timeout_s: float) -> list[str]:
    # SIGINT lets the CLI end its turn cleanly; the hard kill follows after KILL_AFTER_S.
    seconds = max(1, math.ceil(timeout_s))
    return ["timeout", "--signal=INT", f"--kill-after={KILL_AFTER_S}", str(seconds)]


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
