from dataclasses import dataclass, replace
from enum import StrEnum

from skeleton_crew.domain.settings import AppSettings

RUNNER_PREFIX = "runner-"
CRASH_WINDOW_S = 60.0
_BASE_DELAY_S = 10.0
MAX_RESTART_DELAY_S = 900.0
_MAX_BACKOFF_EXPONENT = 16
PUBLIC_REPO_PROBLEM = (
    "self-hosted CI is only available for private repositories: "
    "jobs from fork pull requests would run on this host"
)
NO_RUNNER_REGISTERED = "self-hosted CI is on, but no runner is registered"
CI_OFF_REASON = "self-hosted CI is off"
KILL_SWITCH_REASON = "kill switch is engaged"
STOPPED_REASON = "stopped"
STANDBY_REASON = "no CI waits on a Skeleton Crew branch"
DRAINING_REASON = "finishing its job"
CRASH_REASON = "the runner stopped right after starting; check its registration"


class RunnerStatus(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    STARTING = "starting"
    STANDBY = "standby"
    OFFLINE = "offline"
    NOT_REGISTERED = "not registered"


@dataclass(frozen=True)
class RunnerState:
    name: str
    status: RunnerStatus
    reason: str = ""


@dataclass(frozen=True)
class RunnerSlot:
    started_at: float = 0.0
    failures: int = 0
    not_before: float = 0.0
    reason: str = ""


def eligibility_problem(*, private: bool) -> str | None:
    return None if private else PUBLIC_REPO_PROBLEM


def unreadable_visibility(error: str) -> str:
    return f"cannot read the repository visibility: {error}"


def is_definite(problem: str | None) -> bool:
    return problem in (None, PUBLIC_REPO_PROBLEM)


def settings_blocker(settings: AppSettings) -> str | None:
    if not settings.ci.enabled:
        return CI_OFF_REASON
    if settings.kill_switch:
        return KILL_SWITCH_REASON
    return None


def standby_state(runner: str, *, registered: bool) -> RunnerState:
    if registered:
        return RunnerState(runner, RunnerStatus.STANDBY, STANDBY_REASON)
    return RunnerState(runner, RunnerStatus.NOT_REGISTERED)


def runner_names(count: int) -> list[str]:
    return [f"{RUNNER_PREFIX}{n}" for n in range(1, count + 1)]


def is_crash(ran_s: float) -> bool:
    return ran_s < CRASH_WINDOW_S


def restart_delay(failures: int) -> float:
    if failures <= 0:
        return 0.0
    return min(
        MAX_RESTART_DELAY_S, _BASE_DELAY_S * float(2 ** min(failures - 1, _MAX_BACKOFF_EXPONENT))
    )


def after_exit(slot: RunnerSlot, now: float) -> RunnerSlot:
    if is_crash(now - slot.started_at):
        slot = replace(slot, failures=slot.failures + 1, reason=CRASH_REASON)
    else:
        slot = replace(slot, failures=0)
    return replace(slot, not_before=now + restart_delay(slot.failures))


def after_start_failure(slot: RunnerSlot, now: float, error: str) -> RunnerSlot:
    failures = slot.failures + 1
    return replace(slot, failures=failures, not_before=now + restart_delay(failures), reason=error)


def retry_reason(slot: RunnerSlot, now: float) -> str | None:
    if now < slot.not_before:
        return f"{slot.reason} (retry in {slot.not_before - now:.0f}s)"
    return None
