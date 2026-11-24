from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, Self

from skeleton_crew.domain.ci_policy import CiState, CiStatus
from skeleton_crew.domain.settings import Limits
from skeleton_crew.github.gateway import Gateway


class Timer(Protocol):
    @property
    def clock(self) -> Callable[[], datetime]: ...

    @property
    def sleep(self) -> Callable[[float], Awaitable[None]]: ...


@dataclass(frozen=True)
class CiWait:
    required: Collection[str]
    poll_s: float
    timeout_s: float
    grace_s: float

    @classmethod
    def from_limits(cls, limits: Limits, required: Collection[str]) -> Self:
        return cls(required, limits.ci_poll_seconds, limits.ci_timeout_s, limits.ci_grace_s)


async def wait_for_ci(gw: Gateway, sha: str, wait: CiWait, timer: Timer) -> CiStatus:
    started = timer.clock()
    deadline = started + timedelta(seconds=wait.timeout_s)
    grace_end = started + timedelta(seconds=wait.grace_s)
    while True:
        status = await gw.ci_status(sha, wait.required)
        now = timer.clock()
        if status.state in (CiState.SUCCESS, CiState.FAILURE):
            return status
        if status.state is CiState.NONE and now >= grace_end:
            return status
        if now >= deadline:
            return CiStatus(CiState.PENDING, status.failed_jobs)
        await timer.sleep(wait.poll_s)
