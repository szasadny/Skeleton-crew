from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from skeleton_crew.domain.errors import InvalidTransition
from skeleton_crew.domain.settings import Limits


class RunTrigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"


class TaskState(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    CODING = "coding"
    POLICY_CHECK = "policy_check"
    AWAITING_CI = "awaiting_ci"
    REVIEWING = "reviewing"
    DONE = "done"
    ESCALATED = "escalated"
    FAILED = "failed"
    ABORTED = "aborted"


class TaskKind(StrEnum):
    ISSUE = "issue"
    PR_FIX = "pr_fix"


class TaskEvent(StrEnum):
    STARTED = "started"
    PREPARED = "prepared"
    CODE_DONE = "code_done"
    POLICY_PASSED = "policy_passed"
    POLICY_FAILED = "policy_failed"
    CHECK_FAILED = "check_failed"
    CI_PASSED = "ci_passed"
    CI_FAILED = "ci_failed"
    REVIEW_APPROVED = "review_approved"
    REVIEW_CHANGES = "review_changes"
    ESCALATE = "escalate"
    ERROR = "error"
    ABORT = "abort"


class JobMode(StrEnum):
    FRESH = "fresh"
    CONTINUED = "continued"
    PR_FIX = "pr_fix"


class FollowUpKind(StrEnum):
    CI_FAILED = "ci_failed"
    REVIEW_CHANGES = "review_changes"
    POLICY_FAILED = "policy_failed"
    CHECK_FAILED = "check_failed"


class RefineState(StrEnum):
    QUEUED = "queued"
    REFINING = "refining"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"


class RunState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PREFLIGHT_FAILED = "preflight_failed"
    FAILED = "failed"
    ABORTED = "aborted"


class RunStep(StrEnum):
    PREFLIGHT = "preflight"
    CANDIDATES = "candidates"
    SELECTOR = "selector"


TERMINAL_TASK_STATES = frozenset(
    {TaskState.DONE, TaskState.ESCALATED, TaskState.FAILED, TaskState.ABORTED}
)

TERMINAL_REFINE_STATES = frozenset({RefineState.DONE, RefineState.FAILED, RefineState.ABORTED})

QUOTA_FREE_TASK_STATES = frozenset({TaskState.ABORTED})

NOTE_CHARS = 1000

_TRANSITIONS: dict[tuple[TaskState, TaskEvent], TaskState] = {
    (TaskState.QUEUED, TaskEvent.STARTED): TaskState.PREPARING,
    (TaskState.PREPARING, TaskEvent.PREPARED): TaskState.CODING,
    (TaskState.CODING, TaskEvent.CODE_DONE): TaskState.POLICY_CHECK,
    (TaskState.POLICY_CHECK, TaskEvent.POLICY_PASSED): TaskState.AWAITING_CI,
    (TaskState.POLICY_CHECK, TaskEvent.POLICY_FAILED): TaskState.CODING,
    (TaskState.POLICY_CHECK, TaskEvent.CHECK_FAILED): TaskState.CODING,
    (TaskState.AWAITING_CI, TaskEvent.CI_PASSED): TaskState.REVIEWING,
    (TaskState.AWAITING_CI, TaskEvent.CI_FAILED): TaskState.CODING,
    (TaskState.REVIEWING, TaskEvent.REVIEW_APPROVED): TaskState.DONE,
    (TaskState.REVIEWING, TaskEvent.REVIEW_CHANGES): TaskState.CODING,
}

_TO_ANY_NON_TERMINAL: dict[TaskEvent, TaskState] = {
    TaskEvent.ESCALATE: TaskState.ESCALATED,
    TaskEvent.ERROR: TaskState.FAILED,
    TaskEvent.ABORT: TaskState.ABORTED,
}


def transition(state: TaskState, event: TaskEvent) -> TaskState:
    if state in TERMINAL_TASK_STATES:
        raise InvalidTransition(f"{state} is terminal")
    if event in _TO_ANY_NON_TERMINAL:
        return _TO_ANY_NON_TERMINAL[event]
    try:
        return _TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition(f"{event} not allowed in {state}") from None


def merge_blockers(existing: Sequence[str], new: Iterable[str], cap: int) -> list[str]:
    added = [b for b in dict.fromkeys(new) if b not in existing]
    return [*existing, *added[: max(cap - len(existing), 0)]]


def cap_note(note: str) -> str:
    return note[:NOTE_CHARS]


@dataclass(frozen=True)
class RoundRule:
    limit: Callable[[Limits], int]
    event: TaskEvent
    counter_field: str
    exhausted_text: str


ROUND_RULES: Mapping[FollowUpKind, RoundRule] = {
    FollowUpKind.CHECK_FAILED: RoundRule(
        lambda limits: limits.max_check_rounds,
        TaskEvent.CHECK_FAILED,
        "check_rounds",
        "the check command still fails after {limit} fix rounds",
    ),
    FollowUpKind.POLICY_FAILED: RoundRule(
        lambda limits: limits.max_policy_rounds,
        TaskEvent.POLICY_FAILED,
        "policy_rounds",
        "the change still breaks policy after {limit} fix rounds",
    ),
    FollowUpKind.CI_FAILED: RoundRule(
        lambda limits: limits.max_ci_rounds,
        TaskEvent.CI_FAILED,
        "ci_rounds",
        "CI still fails after {limit} fix rounds",
    ),
    FollowUpKind.REVIEW_CHANGES: RoundRule(
        lambda limits: limits.max_review_rounds,
        TaskEvent.REVIEW_CHANGES,
        "review_rounds",
        "the reviewer still requests changes after {limit} fix rounds",
    ),
}


def rounds_exhausted(kind: FollowUpKind, rounds: int, limits: Limits) -> str | None:
    rule = ROUND_RULES[kind]
    limit = rule.limit(limits)
    return rule.exhausted_text.format(limit=limit) if rounds > limit else None


def works_on_existing_branch(mode: JobMode) -> bool:
    return mode is not JobMode.FRESH
