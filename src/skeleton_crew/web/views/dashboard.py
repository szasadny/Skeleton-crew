from collections import Counter
from collections.abc import Iterable
from typing import Any

from skeleton_crew.domain.runner_policy import RunnerState
from skeleton_crew.services import status_service
from skeleton_crew.web.state import SessionDep, WebState


def runner_counts(runners: Iterable[RunnerState]) -> list[tuple[str, int]]:
    return sorted(Counter(runner.status.value for runner in runners).items())


async def status_context(state: WebState, session: SessionDep) -> dict[str, Any]:
    status = await status_service.crew_status(session)
    return {
        **vars(status),
        "step": state.controller.step,
        "next_run": state.scheduler.next_run_time(),
        "runners": runner_counts(state.runners.statuses()),
    }
