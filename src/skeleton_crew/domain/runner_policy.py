from dataclasses import dataclass
from enum import StrEnum

RUNNER_PREFIX = "runner-"
CRASH_WINDOW_S = 60.0
_BASE_DELAY_S = 10.0
MAX_RESTART_DELAY_S = 900.0
PUBLIC_REPO_PROBLEM = (
    "self-hosted CI is only available for private repositories: "
    "jobs from fork pull requests would run on this host"
)
STANDBY_REASON = "no Skeleton Crew task waits for CI"


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


def eligibility_problem(*, private: bool) -> str | None:
    return None if private else PUBLIC_REPO_PROBLEM


def runner_names(count: int) -> list[str]:
    return [f"{RUNNER_PREFIX}{n}" for n in range(1, count + 1)]


def is_crash(ran_s: float) -> bool:
    return ran_s < CRASH_WINDOW_S


def restart_delay(failures: int) -> float:
    if failures <= 0:
        return 0.0
    return min(MAX_RESTART_DELAY_S, _BASE_DELAY_S * float(2 ** min(failures - 1, 16)))
