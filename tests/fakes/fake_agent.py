import asyncio
import json
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from skeleton_crew.agents.base import ExecOutcome, SessionOutcome, SessionRequest, failed_outcome
from skeleton_crew.domain.settings import Role, RoleAgent


def ok_outcome(structured_output: str, *, session_id: str = "fake-session") -> SessionOutcome:
    return SessionOutcome(
        session_id=session_id,
        structured_output=structured_output,
        cost_usd=0.01,
        turns=1,
        ok=True,
        error=None,
        timed_out=False,
    )


def stopped_outcome(
    *, session_id: str | None = "worker-session", reason: str = "hook_stopped"
) -> SessionOutcome:
    return replace(
        failed_outcome("no structured output"),
        session_id=session_id,
        cost_usd=0.01,
        turns=2,
        stopped_early=reason,
    )


def limited_outcome(*, session_id: str | None = "worker-session") -> SessionOutcome:
    return replace(
        failed_outcome("session reported an error"),
        session_id=session_id,
        cost_usd=0.03,
        turns=1,
        usage_limited=True,
    )


class FakeBackend:
    def __init__(
        self,
        outcomes: Mapping[Role, Sequence[SessionOutcome | str]] | None = None,
        *,
        name: str = "fake",
        env: Mapping[str, str] | None = None,
        problems: Sequence[str] = (),
        egress: tuple[str, ...] = ("fake.example.com",),
    ) -> None:
        self.name = name
        self.requests: list[SessionRequest] = []
        self._queues: dict[Role, deque[SessionOutcome | str]] = defaultdict(deque)
        for role, items in (outcomes or {}).items():
            self._queues[role].extend(items)
        self._env = dict(env or {})
        self._problems = list(problems)
        self._egress = egress

    def script(self, role: Role, *items: SessionOutcome | str) -> None:
        self._queues[role].extend(items)

    def egress_domains(self) -> tuple[str, ...]:
        return self._egress

    def auto_loaded_docs(self) -> tuple[str, ...]:
        return ("CLAUDE.md",)

    def credential_problems(self) -> list[str]:
        return list(self._problems)

    def env(self) -> dict[str, str]:
        return dict(self._env)

    def command(self, req: SessionRequest) -> list[str]:
        self.requests.append(req)
        return ["fake-agent", req.role.value]

    def parse(self, stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome:
        if timed_out:
            return failed_outcome("timed out", timed_out=True)
        queue = self._queues[self.requests[-1].role]
        if not queue:
            return failed_outcome("fake: no scripted outcome")
        item = queue.popleft()
        return ok_outcome(item) if isinstance(item, str) else item


@dataclass(frozen=True)
class ExecStep:
    stdout: str = ""
    exit_code: int = 0
    timed_out: bool = False
    stderr_tail: str = ""
    error: Exception | None = None


@dataclass(frozen=True)
class ExecCall:
    argv: list[str]
    env: dict[str, str]
    cwd: str
    timeout_s: float
    stdout_path: Path


@dataclass
class FakeExecutor:
    steps: list[ExecStep] = field(default_factory=list)
    default: ExecStep = field(default_factory=ExecStep)
    calls: list[ExecCall] = field(default_factory=list)

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        timeout_s: float,
        stdout_path: Path,
    ) -> ExecOutcome:
        self.calls.append(ExecCall(list(argv), dict(env), cwd, timeout_s, stdout_path))
        step = self.steps.pop(0) if self.steps else self.default
        await asyncio.to_thread(stdout_path.write_text, step.stdout, encoding="utf-8")
        if step.error is not None:
            raise step.error
        return ExecOutcome(step.exit_code, step.timed_out, step.stderr_tail)


def claude_result_line(**fields: object) -> str:
    event: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "4f1c2d3e-0000-4000-8000-000000000001",
        "total_cost_usd": 0.42,
        "num_turns": 7,
        "result": "Done.",
        "permission_denials": [],
    }
    event.update(fields)
    return json.dumps(event)


def make_request(
    role: Role = Role.WORKER,
    *,
    prompt: str = "Do the task.",
    timeout_s: float = 3600.0,
    resume_session_id: str | None = None,
    max_budget_usd: float | None = None,
) -> SessionRequest:
    return SessionRequest(
        role=role,
        prompt=prompt,
        agent=RoleAgent(),
        max_turns=150,
        timeout_s=timeout_s,
        json_schema='{"type":"object"}',
        rules_path_in_container="/skeleton-in/rules.md",
        prompt_path_in_container="/skeleton-in/prompt.md",
        schema_path_in_container="/skeleton-in/schema.json",
        cwd="/workspace",
        resume_session_id=resume_session_id,
        max_budget_usd=max_budget_usd,
    )
