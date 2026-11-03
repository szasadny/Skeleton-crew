from typing import Literal


class FakeRunControl:
    def __init__(self, start_result: int | None = 1) -> None:
        self.start_result = start_result
        self.running = False
        self.starts: list[str] = []
        self.kills = 0

    @property
    def is_running(self) -> bool:
        return self.running

    async def start(self, trigger: Literal["schedule", "manual"]) -> int | None:
        self.starts.append(trigger)
        return self.start_result

    async def kill(self) -> None:
        self.kills += 1
