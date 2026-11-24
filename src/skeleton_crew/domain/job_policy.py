from datetime import datetime, timedelta
from enum import StrEnum

from skeleton_crew.domain.ci_policy import CiState
from skeleton_crew.domain.task_state import FollowUpKind

# Produced by the repo's code and dependencies, not by a trusted person.
REPO_OUTPUT_KINDS = frozenset({FollowUpKind.CI_FAILED, FollowUpKind.CHECK_FAILED})
UNTRUSTED_OUTPUT_CHARS = 20_000


class CiResult(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    NOT_CONFIGURED = "no CI configured"
    DRY_RUN = "skipped (dry run, nothing was pushed)"
    NOT_REPORTED = "not reported"
    TIMED_OUT = "timed out"


class ReviewStep(StrEnum):
    APPROVED = "approved"
    TOO_LARGE = "too_large"
    CHANGES = "changes"


_CI_RESULTS = {
    CiState.SUCCESS: CiResult.SUCCESS,
    CiState.FAILURE: CiResult.FAILURE,
    CiState.PENDING: CiResult.TIMED_OUT,
}


def is_repo_output(kind: FollowUpKind) -> bool:
    return kind in REPO_OUTPUT_KINDS


def check_to_run(command: str | None, resolving_merge: bool) -> str | None:
    # While resolving a merge only conflicted files may change; CI reports the rest.
    return None if resolving_merge else command or None


def ci_result(state: CiState, require_ci: bool) -> CiResult:
    if state is CiState.NONE:
        return CiResult.NOT_REPORTED if require_ci else CiResult.NOT_CONFIGURED
    return _CI_RESULTS[state]


def review_step(verdict: str) -> ReviewStep:
    if verdict == "approve":
        return ReviewStep.APPROVED
    if verdict == "scope_too_large":
        return ReviewStep.TOO_LARGE
    return ReviewStep.CHANGES


def worker_finished(status: str) -> bool:
    return status == "done"


def may_report_new_issue(reported: int, cap: int) -> bool:
    return reported < cap


def may_start_work(halted: bool, now: datetime, run_deadline: datetime) -> bool:
    return not halted and now < run_deadline


def job_seconds_left(now: datetime, timeout_hours: float, run_deadline: datetime) -> float:
    deadline = min(now + timedelta(hours=timeout_hours), run_deadline)
    return max((deadline - now).total_seconds(), 0.0)
