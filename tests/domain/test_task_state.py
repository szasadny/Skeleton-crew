import itertools

import pytest

from skeleton_crew.domain.errors import InvalidTransition
from skeleton_crew.domain.task_state import (
    QUOTA_FREE_TASK_STATES,
    TERMINAL_TASK_STATES,
    RunTrigger,
    TaskEvent,
    TaskState,
    merge_blockers,
    transition,
)

HAPPY_PATH = [
    (TaskState.QUEUED, TaskEvent.STARTED, TaskState.PREPARING),
    (TaskState.PREPARING, TaskEvent.PREPARED, TaskState.CODING),
    (TaskState.CODING, TaskEvent.CODE_DONE, TaskState.POLICY_CHECK),
    (TaskState.POLICY_CHECK, TaskEvent.POLICY_PASSED, TaskState.AWAITING_CI),
    (TaskState.AWAITING_CI, TaskEvent.CI_PASSED, TaskState.REVIEWING),
    (TaskState.REVIEWING, TaskEvent.REVIEW_APPROVED, TaskState.DONE),
]
LOOPS_BACK = [
    (TaskState.POLICY_CHECK, TaskEvent.POLICY_FAILED, TaskState.CODING),
    (TaskState.POLICY_CHECK, TaskEvent.CHECK_FAILED, TaskState.CODING),
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


def test_queued_task_cannot_skip_preparing() -> None:
    with pytest.raises(InvalidTransition):
        transition(TaskState.QUEUED, TaskEvent.PREPARED)


@pytest.mark.parametrize("state", [s for s in NON_TERMINAL if s is not TaskState.QUEUED])
def test_only_a_queued_task_can_start(state: TaskState) -> None:
    with pytest.raises(InvalidTransition):
        transition(state, TaskEvent.STARTED)


@pytest.mark.parametrize("state", [s for s in NON_TERMINAL if s is not TaskState.POLICY_CHECK])
def test_check_failure_only_follows_a_policy_check(state: TaskState) -> None:
    with pytest.raises(InvalidTransition):
        transition(state, TaskEvent.CHECK_FAILED)


def test_terminal_set() -> None:
    assert (
        frozenset({TaskState.DONE, TaskState.ESCALATED, TaskState.FAILED, TaskState.ABORTED})
        == TERMINAL_TASK_STATES
    )


def test_run_triggers_keep_their_stored_values() -> None:
    assert [trigger.value for trigger in RunTrigger] == ["schedule", "manual"]


def test_only_aborted_tasks_are_free_of_quota() -> None:
    assert {TaskState.ABORTED} == QUOTA_FREE_TASK_STATES
    assert QUOTA_FREE_TASK_STATES <= TERMINAL_TASK_STATES


def test_merge_blockers_dedupes_and_keeps_existing_order() -> None:
    assert merge_blockers(["a"], ["b", "a", "b", "c"], 10) == ["a", "b", "c"]


def test_merge_blockers_caps_new_entries() -> None:
    assert merge_blockers(["a", "b"], ["c", "d", "e"], 3) == ["a", "b", "c"]
    assert merge_blockers(["a", "b", "c"], ["d"], 3) == ["a", "b", "c"]
    assert merge_blockers(["a", "b", "c", "d"], ["e"], 3) == ["a", "b", "c", "d"]
    assert merge_blockers([], [], 3) == []
