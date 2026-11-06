from typing import Literal, Protocol

from skeleton_crew.domain.runner_policy import RunnerState


class RunControl(Protocol):
    @property
    def is_running(self) -> bool: ...

    async def start(self, trigger: Literal["schedule", "manual"]) -> int | None: ...

    async def kill(self) -> None: ...


class RunnerControl(Protocol):
    def statuses(self) -> list[RunnerState]: ...
