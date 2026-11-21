from typing import Literal, Protocol

from skeleton_crew.domain.runner_policy import RunnerState
from skeleton_crew.domain.task_state import RunStep


class RunControl(Protocol):
    @property
    def is_running(self) -> bool: ...

    @property
    def step(self) -> RunStep | None: ...

    async def start(self, trigger: Literal["schedule", "manual"]) -> int | None: ...

    async def kill(self) -> None: ...


class RunnerControl(Protocol):
    def statuses(self) -> list[RunnerState]: ...
