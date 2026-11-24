import asyncio
import json
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from skeleton_crew.agents.backends._shared import NO_STRUCTURED_OUTPUT, REPORTED_ERROR
from skeleton_crew.agents.base import ExecOutcome, SessionOutcome, SessionRequest, failed_outcome
from skeleton_crew.domain.settings import Role, RoleAgent
from skeleton_crew.pipeline.sandbox import INBOX, RULES_FILE
from skeleton_crew.pipeline.sessions import PROMPT_FILE
from skeleton_crew.runtime.containers import WORKSPACE

RULES_PATH = f"{INBOX}/{RULES_FILE}"
PROMPT_PATH = f"{INBOX}/{PROMPT_FILE}"
FAKE_AGENT = "fake-agent"
MODEL_DOMAIN = "model.example.com"
MODEL_TOKEN = "model-token-for-tests"  # noqa: S105


def ok_outcome(structured_output: str, *, session_id: str = "worker-session") -> SessionOutcome:
    return SessionOutcome(
        session_id=session_id,
        structured_output=structured_output,
        cost_usd=0.01,
        turns=1,
        error=None,
        timed_out=False,
    )


def stopped_outcome(
    *, session_id: str | None = "worker-session", reason: str = "hook_stopped"
) -> SessionOutcome:
    return replace(
        failed_outcome(NO_STRUCTURED_OUTPUT),
        session_id=session_id,
        cost_usd=0.01,
        turns=2,
        stopped_early=reason,
    )


def limited_outcome(*, session_id: str | None = "worker-session") -> SessionOutcome:
    return replace(
        failed_outcome(REPORTED_ERROR),
        session_id=session_id,
        cost_usd=0.03,
        turns=1,
        usage_limited=True,
    )


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.requests: list[SessionRequest] = []
        self.problems: list[str] = []
        self.auto_loaded: tuple[str, ...] = ()
        self._queues: dict[Role, deque[SessionOutcome | str]] = defaultdict(deque)

    def script(self, role: Role, *items: SessionOutcome | str) -> None:
        self._queues[role].extend(items)

    def egress_domains(self) -> tuple[str, ...]:
        return (MODEL_DOMAIN,)

    def auto_loaded_docs(self) -> tuple[str, ...]:
        return self.auto_loaded

    def credential_problems(self) -> list[str]:
        return list(self.problems)

    def env(self) -> dict[str, str]:
        return {"MODEL_TOKEN": MODEL_TOKEN}

    def command(self, req: SessionRequest) -> list[str]:
        self.requests.append(req)
        return [FAKE_AGENT, req.role.value]

    def parse(self, stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome:
        # The role comes from stdout: parallel sessions make the last request the wrong one.
        queue = self._queues[Role(stdout_path.read_text())]
        if not queue:
            return failed_outcome("no scripted outcome")
        item = queue.popleft()
        return ok_outcome(item) if isinstance(item, str) else item

    def prompts(self, role: Role) -> list[SessionRequest]:
        return [r for r in self.requests if r.role is role]


@dataclass(frozen=True)
class ExecStep:
    stdout: str = ""
    exit_code: int = 0
    timed_out: bool = False
    stderr_tail: str = ""
    error: Exception | None = None


@dataclass(frozen=True)
class ExecutorCall:
    argv: list[str]
    env: dict[str, str]
    cwd: str
    timeout_s: float
    stdout_path: Path


@dataclass
class FakeExecutor:
    steps: list[ExecStep] = field(default_factory=list)
    default: ExecStep = field(default_factory=ExecStep)
    calls: list[ExecutorCall] = field(default_factory=list)

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        timeout_s: float,
        stdout_path: Path,
    ) -> ExecOutcome:
        self.calls.append(ExecutorCall(list(argv), dict(env), cwd, timeout_s, stdout_path))
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
        rules_path_in_container=RULES_PATH,
        prompt_path_in_container=PROMPT_PATH,
        cwd=WORKSPACE,
        resume_session_id=resume_session_id,
        max_budget_usd=max_budget_usd,
    )
