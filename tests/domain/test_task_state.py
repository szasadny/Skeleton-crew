import itertools

import pytest

from skeleton_crew.domain.errors import InvalidTransition
from skeleton_crew.domain.task_state import (
    TERMINAL_TASK_STATES,
    TaskEvent,
    TaskState,
    transition,
)

HAPPY_PATH = [
    (TaskState.QUEUED, TaskEvent.PREPARED, TaskState.PREPARING),
    (TaskState.PREPARING, TaskEvent.PREPARED, TaskState.CODING),
    (TaskState.CODING, TaskEvent.CODE_DONE, TaskState.POLICY_CHECK),
    (TaskState.POLICY_CHECK, TaskEvent.POLICY_PASSED, TaskState.AWAITING_CI),
    (TaskState.AWAITING_CI, TaskEvent.CI_PASSED, TaskState.REVIEWING),
    (TaskState.REVIEWING, TaskEvent.REVIEW_APPROVED, TaskState.DONE),
]
LOOPS_BACK = [
    (TaskState.POLICY_CHECK, TaskEvent.POLICY_FAILED, TaskState.CODING),
    (TaskState.AWAITING_CI, TaskEvent.CI_FAILED, TaskState.CODING),
    (TaskState.REVIEWING, TaskEvent.REVIEW_CHANGES, TaskState.CODING),
]
NON_TERMINAL = [s for s in TaskState if s not in TERMINAL_TASK_STATES]
EXIT_EVENTS = {
    TaskEvent.ESCALATE: TaskState.ESCALATED,
    TaskEvent.ERROR: TaskState.FAILED,
    TaskEvent.ABORT: TaskState.ABORTED,
}
ALLOWED = {(s, e) for s, e, _ in HAPPY_PATH + LOOPS_BACK} | {
    (s, e) for s in NON_TERMINAL for e in EXIT_EVENTS
}


@pytest.mark.parametrize(("state", "event", "target"), HAPPY_PATH + LOOPS_BACK)
def test_defined_transitions(state: TaskState, event: TaskEvent, target: TaskState) -> None:
    assert transition(state, event) is target


@pytest.mark.parametrize(("state", "event"), list(itertools.product(NON_TERMINAL, EXIT_EVENTS)))
def test_any_non_terminal_state_can_exit(state: TaskState, event: TaskEvent) -> None:
    assert transition(state, event) is EXIT_EVENTS[event]


@pytest.mark.parametrize(
    ("state", "event"), list(itertools.product(TERMINAL_TASK_STATES, TaskEvent))
)
def test_terminal_states_reject_every_event(state: TaskState, event: TaskEvent) -> None:
    with pytest.raises(InvalidTransition, match="terminal"):
        transition(state, event)


@pytest.mark.parametrize(
    ("state", "event"),
    [(s, e) for s, e in itertools.product(NON_TERMINAL, TaskEvent) if (s, e) not in ALLOWED],
)
def test_undefined_transitions_are_rejected(state: TaskState, event: TaskEvent) -> None:
    with pytest.raises(InvalidTransition):
        transition(state, event)


def test_coding_cannot_skip_policy_check() -> None:
    with pytest.raises(InvalidTransition):
        transition(TaskState.CODING, TaskEvent.POLICY_PASSED)
    with pytest.raises(InvalidTransition):
        transition(TaskState.CODING, TaskEvent.REVIEW_APPROVED)


def test_terminal_set() -> None:
    assert (
        frozenset({TaskState.DONE, TaskState.ESCALATED, TaskState.FAILED, TaskState.ABORTED})
        == TERMINAL_TASK_STATES
    )
