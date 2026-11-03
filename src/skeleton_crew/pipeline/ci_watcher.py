from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from skeleton_crew.github.gateway import CiState, CiStatus, Gateway


async def wait_for_ci(
    gw: Gateway,
    sha: str,
    *,
    poll_s: float,
    timeout_s: float,
    grace_s: float,
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], datetime],
) -> CiStatus:
    started = clock()
    deadline = started + timedelta(seconds=timeout_s)
    grace_end = started + timedelta(seconds=grace_s)
    while True:
        status = await gw.ci_status(sha)
        now = clock()
        if status.state in (CiState.SUCCESS, CiState.FAILURE):
            return status
        if status.state is CiState.NONE and now >= grace_end:
            return status
        if now >= deadline:
            return CiStatus(CiState.PENDING, status.failed_jobs)
        await sleep(poll_s)
