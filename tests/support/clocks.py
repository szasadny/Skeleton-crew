from datetime import UTC, datetime, timedelta

START = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
MONOTONIC_START = 1000.0


class FakeClock:
    def __init__(self) -> None:
        self.now = START
        self.ticks = MONOTONIC_START

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.ticks

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.ticks += seconds
