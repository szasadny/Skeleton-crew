from enum import StrEnum

from skeleton_crew.domain.errors import InvalidTransition


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
    PR_UPDATE = "pr_update"
    PR_FIX = "pr_fix"


class TaskEvent(StrEnum):
    PREPARED = "prepared"
    CODE_DONE = "code_done"
    POLICY_PASSED = "policy_passed"
    POLICY_FAILED = "policy_failed"
    CI_PASSED = "ci_passed"
    CI_FAILED = "ci_failed"
    REVIEW_APPROVED = "review_approved"
    REVIEW_CHANGES = "review_changes"
    ESCALATE = "escalate"
    ERROR = "error"
    ABORT = "abort"


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


TERMINAL_TASK_STATES = frozenset(
    {TaskState.DONE, TaskState.ESCALATED, TaskState.FAILED, TaskState.ABORTED}
)

TERMINAL_REFINE_STATES = frozenset({RefineState.DONE, RefineState.FAILED, RefineState.ABORTED})

_TRANSITIONS: dict[tuple[TaskState, TaskEvent], TaskState] = {
    (TaskState.QUEUED, TaskEvent.PREPARED): TaskState.PREPARING,
    (TaskState.PREPARING, TaskEvent.PREPARED): TaskState.CODING,
    (TaskState.CODING, TaskEvent.CODE_DONE): TaskState.POLICY_CHECK,
    (TaskState.POLICY_CHECK, TaskEvent.POLICY_PASSED): TaskState.AWAITING_CI,
    (TaskState.POLICY_CHECK, TaskEvent.POLICY_FAILED): TaskState.CODING,
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
