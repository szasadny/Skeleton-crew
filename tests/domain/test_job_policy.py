from datetime import UTC, datetime, timedelta

import pytest

from skeleton_crew.domain.ci_policy import CiState
from skeleton_crew.domain.job_policy import (
    CiResult,
    ReviewStep,
    check_to_run,
    ci_result,
    is_repo_output,
    job_seconds_left,
    may_report_new_issue,
    may_start_work,
    review_step,
    worker_finished,
)
from skeleton_crew.domain.task_state import FollowUpKind

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def test_only_ci_and_check_output_count_as_repo_output() -> None:
    assert {k for k in FollowUpKind if is_repo_output(k)} == {
        FollowUpKind.CI_FAILED,
        FollowUpKind.CHECK_FAILED,
    }


def test_the_check_command_runs_outside_a_merge() -> None:
    assert check_to_run("make check", resolving_merge=False) == "make check"


@pytest.mark.parametrize(
    ("command", "resolving"), [("make check", True), (None, False), ("", False)]
)
def test_no_check_while_resolving_a_merge_or_without_a_command(
    command: str | None, resolving: bool
) -> None:
    assert check_to_run(command, resolving) is None


@pytest.mark.parametrize(
    ("state", "require_ci", "expected"),
    [
        (CiState.SUCCESS, True, CiResult.SUCCESS),
        (CiState.FAILURE, False, CiResult.FAILURE),
        (CiState.PENDING, False, CiResult.TIMED_OUT),
        (CiState.NONE, True, CiResult.NOT_REPORTED),
        (CiState.NONE, False, CiResult.NOT_CONFIGURED),
    ],
)
def test_ci_states_map_to_one_result(state: CiState, require_ci: bool, expected: CiResult) -> None:
    assert ci_result(state, require_ci) is expected


def test_the_reviewer_sees_the_same_ci_texts_as_before() -> None:
    assert CiResult.SUCCESS.value == CiState.SUCCESS.value
    assert CiResult.NOT_CONFIGURED.value == "no CI configured"
    assert CiResult.DRY_RUN.value == "skipped (dry run, nothing was pushed)"


@pytest.mark.parametrize(
    ("verdict", "step"),
    [
        ("approve", ReviewStep.APPROVED),
        ("scope_too_large", ReviewStep.TOO_LARGE),
        ("changes_requested", ReviewStep.CHANGES),
        ("Approve", ReviewStep.CHANGES),
    ],
)
def test_review_verdicts(verdict: str, step: ReviewStep) -> None:
    assert review_step(verdict) is step


@pytest.mark.parametrize("status", ["scope_too_large", "blocked", "already_fixed", "Done", ""])
def test_only_done_finishes_the_worker(status: str) -> None:
    assert worker_finished("done")
    assert not worker_finished(status)


def test_new_issues_stop_at_the_cap() -> None:
    assert may_report_new_issue(1, 2)
    assert not may_report_new_issue(2, 2)
    assert not may_report_new_issue(0, 0)


def test_the_job_ends_at_its_own_timeout_or_the_run_window() -> None:
    assert job_seconds_left(NOW, 1.0, NOW + timedelta(hours=5)) == 3600.0
    assert job_seconds_left(NOW, 5.0, NOW + timedelta(minutes=10)) == 600.0
    assert job_seconds_left(NOW, 5.0, NOW - timedelta(minutes=1)) == 0.0


def test_new_work_starts_only_before_the_window_ends_and_while_not_halted() -> None:
    later = NOW + timedelta(seconds=1)
    assert may_start_work(False, NOW, later)
    assert not may_start_work(True, NOW, later)
    assert not may_start_work(False, later, later)
