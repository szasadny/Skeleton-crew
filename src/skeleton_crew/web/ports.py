from typing import Literal, Protocol


class RunControl(Protocol):
    @property
    def is_running(self) -> bool: ...

    async def start(self, trigger: Literal["schedule", "manual"]) -> int | None: ...

    async def kill(self) -> None: ...
