from datetime import timedelta

from fake_gateway import FakeGitHubGateway
from pipeline_helpers import FakeClock

from skeleton_crew.github.gateway import CiState
from skeleton_crew.pipeline.ci_watcher import wait_for_ci

SHA = "a" * 40


class Watch:
    def __init__(self, *states: CiState) -> None:
        self.gateway = FakeGitHubGateway()
        self.clock = FakeClock()
        self.sleeps: list[float] = []
        if states:
            self.gateway.script_ci(SHA, *states)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)

    async def wait(self) -> CiState:
        status = await wait_for_ci(
            self.gateway,
            SHA,
            poll_s=60,
            timeout_s=600,
            grace_s=180,
            sleep=self.sleep,
            clock=self.clock,
        )
        return status.state


async def test_returns_success_after_pending_polls() -> None:
    watch = Watch(CiState.NONE, CiState.PENDING, CiState.SUCCESS)
    assert await watch.wait() is CiState.SUCCESS
    assert watch.sleeps == [60, 60]


async def test_no_checks_after_grace_returns_none() -> None:
    watch = Watch()
    start = watch.clock.now
    assert await watch.wait() is CiState.NONE
    assert watch.clock.now - start == timedelta(seconds=180)


async def test_pending_until_timeout_returns_pending() -> None:
    watch = Watch(CiState.PENDING)
    assert await watch.wait() is CiState.PENDING
    assert len(watch.sleeps) == 10


async def test_failure_is_returned_immediately() -> None:
    watch = Watch(CiState.FAILURE)
    assert await watch.wait() is CiState.FAILURE
    assert watch.sleeps == []
