from pathlib import Path

import pytest
from fake_runtime import argv_starts_with, ok
from pipeline_helpers import Harness
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.domain.branch_policy import BRANCH_PREFIX
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.runner_policy import (
    DRAINING_REASON,
    PUBLIC_REPO_PROBLEM,
    STANDBY_REASON,
    RunnerStatus,
)
from skeleton_crew.domain.settings import SelfHostedCi
from skeleton_crew.domain.task_state import TaskState
from skeleton_crew.pipeline.run_controller import RunController
from skeleton_crew.pipeline.run_setup import preflight
from skeleton_crew.pipeline.runner_pool import (
    BUSY_ARGV,
    CI_DEMAND_TTL_S,
    CONFIG_MOUNT,
    ELIGIBILITY_TTL_S,
    REGISTRATION_FILE,
    RunnerPool,
    runner_spec,
)
from skeleton_crew.runtime.containers import (
    AgentContainerSpec,
    ContainerHandle,
    ContainerStartError,
    ExecResult,
)
from skeleton_crew.store import runs_store

NOT_BUSY = ExecResult(1, None, b"", b"", timed_out=False)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def harness(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> Harness:
    harness = Harness(tmp_path, sessionmaker)
    harness.settings = harness.settings.model_copy(
        update={"ci": SelfHostedCi(enabled=True, runners=2)}
    )
    return harness


@pytest.fixture
def clock() -> Clock:
    return Clock()


async def wait_for_ci(harness: Harness, *, dry_run: bool = False) -> int:
    async with harness.sessionmaker.begin() as session:
        run = await runs_store.create_run(session, "manual", dry_run=dry_run)
        task = await runs_store.add_worker_task(session, run.id, 1, "a")
        await runs_store.update_worker_task(session, task.id, state=TaskState.AWAITING_CI)
        return task.id


async def set_state(harness: Harness, task_id: int, state: TaskState) -> None:
    async with harness.sessionmaker.begin() as session:
        await runs_store.update_worker_task(session, task_id, state=state)


@pytest.fixture
async def waiting_task(harness: Harness) -> int:
    return await wait_for_ci(harness)


@pytest.fixture
def pool(harness: Harness, clock: Clock, waiting_task: int) -> RunnerPool:
    return RunnerPool(harness.deps(), monotonic=clock)


def register(harness: Harness, runner: str) -> Path:
    directory = harness.config.runners_dir / runner
    directory.mkdir(parents=True, exist_ok=True)
    (directory / REGISTRATION_FILE).write_text("{}")
    return directory


def runner_starts(harness: Harness) -> list[AgentContainerSpec]:
    return [s for s in harness.runtime.started if s.image == harness.config.runner_image]


async def tick(harness: Harness, pool: RunnerPool) -> dict[str, RunnerStatus]:
    await harness.save_settings()
    await pool.tick()
    return {state.name: state.status for state in pool.statuses()}


async def test_only_registered_runners_start(harness: Harness, pool: RunnerPool) -> None:
    register(harness, "runner-1")

    states = await tick(harness, pool)

    assert states == {"runner-1": RunnerStatus.STARTING, "runner-2": RunnerStatus.NOT_REGISTERED}
    assert [s.name for s in runner_starts(harness)] == ["skeleton-crew-runner-1"]


async def test_runner_container_is_isolated(harness: Harness, pool: RunnerPool) -> None:
    config_dir = register(harness, "runner-1")

    await tick(harness, pool)

    spec = runner_starts(harness)[0]
    assert spec.network == harness.config.runner_network
    assert [(m.host_path, m.container_path, m.read_only) for m in spec.mounts] == [
        (config_dir, CONFIG_MOUNT, True)
    ]
    assert spec.env["HTTPS_PROXY"] == harness.config.runner_proxy_url
    assert not any("TOKEN" in key or "KEY" in key for key in spec.env)
    assert (spec.cpus, spec.memory_gb, spec.pids) == (2.0, 4.0, 4096)
    assert spec.env["NO_PROXY"] == "localhost,127.0.0.1,::1"


async def test_no_proxy_hosts_reach_the_runner(harness: Harness, pool: RunnerPool) -> None:
    register(harness, "runner-1")
    harness.settings = harness.settings.model_copy(
        update={"ci": SelfHostedCi(enabled=True, no_proxy=("db", "minio", "localhost"))}
    )

    await tick(harness, pool)

    assert runner_starts(harness)[0].env["no_proxy"] == "localhost,127.0.0.1,::1,db,minio"


async def test_a_github_token_in_the_runner_env_is_refused(harness: Harness) -> None:
    spec = runner_spec(harness.deps(), harness.settings, "runner-1")
    leaky = AgentContainerSpec(**{**vars(spec), "env": {**spec.env, "GH_TOKEN": "ghs_x"}})

    with pytest.raises(PolicyViolation):
        await harness.runtime.start(leaky)


async def test_runner_allowlist_goes_to_its_own_proxy(harness: Harness, pool: RunnerPool) -> None:
    register(harness, "runner-1")

    await tick(harness, pool)
    await tick(harness, pool)

    reloads = [c for c in harness.runtime.execs if c.argv[0] == "squid"]
    assert [c.handle.id for c in reloads] == [harness.config.runner_proxy_container]
    allowlist = harness.config.runner_allowlist_path.read_text()
    assert "github.com" in allowlist
    assert not harness.config.egress_allowlist_path.exists()


async def test_running_runner_reports_idle_or_busy(harness: Harness, pool: RunnerPool) -> None:
    register(harness, "runner-1")
    await tick(harness, pool)
    harness.runtime.script(argv_starts_with(*BUSY_ARGV), NOT_BUSY, once=True)
    harness.runtime.script(argv_starts_with(*BUSY_ARGV), ok())

    assert (await tick(harness, pool))["runner-1"] is RunnerStatus.IDLE
    assert (await tick(harness, pool))["runner-1"] is RunnerStatus.BUSY
    assert len(runner_starts(harness)) == 1


async def test_no_task_waiting_for_ci_means_standby(
    harness: Harness, pool: RunnerPool, waiting_task: int
) -> None:
    register(harness, "runner-1")
    await set_state(harness, waiting_task, TaskState.REVIEWING)

    states = await tick(harness, pool)

    assert states == {"runner-1": RunnerStatus.STANDBY, "runner-2": RunnerStatus.NOT_REGISTERED}
    assert pool.statuses()[0].reason == STANDBY_REASON
    assert runner_starts(harness) == []


async def test_runners_start_when_a_task_waits_and_go_when_it_stops(
    harness: Harness, pool: RunnerPool, waiting_task: int
) -> None:
    register(harness, "runner-1")
    await set_state(harness, waiting_task, TaskState.CODING)
    await tick(harness, pool)
    await set_state(harness, waiting_task, TaskState.AWAITING_CI)

    assert (await tick(harness, pool))["runner-1"] is RunnerStatus.STARTING
    assert harness.runtime.running

    await set_state(harness, waiting_task, TaskState.REVIEWING)

    assert (await tick(harness, pool))["runner-1"] is RunnerStatus.STANDBY
    assert harness.runtime.running == {}


async def test_dry_run_task_starts_no_runner(harness: Harness, clock: Clock) -> None:
    register(harness, "runner-1")
    await wait_for_ci(harness, dry_run=True)

    await tick(harness, RunnerPool(harness.deps(), monotonic=clock))

    assert runner_starts(harness) == []


async def test_runner_gets_the_skeleton_branch_prefix(harness: Harness, pool: RunnerPool) -> None:
    register(harness, "runner-1")

    await tick(harness, pool)

    assert runner_starts(harness)[0].env["SC_BRANCH_PREFIX"] == BRANCH_PREFIX


def test_busy_check_matches_the_process_name_not_command_lines() -> None:
    assert "-x" in BUSY_ARGV
    assert "-f" not in BUSY_ARGV


async def test_exited_runner_is_recreated_from_the_image(
    harness: Harness, pool: RunnerPool, clock: Clock
) -> None:
    register(harness, "runner-1")
    await tick(harness, pool)
    first = next(iter(harness.runtime.running.values()))
    clock.now += 600
    harness.runtime.exit(first)

    states = await tick(harness, pool)

    assert states["runner-1"] is RunnerStatus.STARTING
    assert first in harness.runtime.removed
    assert [s.name for s in runner_starts(harness)] == ["skeleton-crew-runner-1"] * 2


async def test_crash_loop_backs_off(harness: Harness, pool: RunnerPool, clock: Clock) -> None:
    register(harness, "runner-1")
    await tick(harness, pool)
    harness.runtime.exit(next(iter(harness.runtime.running.values())))
    clock.now += 5

    states = await tick(harness, pool)

    assert states["runner-1"] is RunnerStatus.OFFLINE
    assert "registration" in pool.statuses()[0].reason
    assert len(runner_starts(harness)) == 1
    clock.now += 60
    assert (await tick(harness, pool))["runner-1"] is RunnerStatus.STARTING


async def test_start_failure_backs_off(harness: Harness, pool: RunnerPool) -> None:
    register(harness, "runner-1")

    async def refuse(spec: AgentContainerSpec) -> ContainerHandle:
        raise ContainerStartError("container skeleton-crew-runner-1 stopped during startup")

    harness.runtime.start = refuse  # type: ignore[method-assign]

    states = await tick(harness, pool)

    assert states["runner-1"] is RunnerStatus.OFFLINE
    assert "stopped during startup" in pool.statuses()[0].reason


@pytest.mark.parametrize(
    "update",
    [{"ci": SelfHostedCi(enabled=False)}, {"kill_switch": True}],
)
async def test_disabled_or_killed_removes_runners_and_starts_none(
    harness: Harness, pool: RunnerPool, update: dict[str, object]
) -> None:
    register(harness, "runner-1")
    await tick(harness, pool)
    harness.settings = harness.settings.model_copy(update=update)

    await tick(harness, pool)
    await tick(harness, pool)

    assert harness.runtime.running == {}
    assert len(runner_starts(harness)) == 1
    assert all(s.status is RunnerStatus.OFFLINE for s in pool.statuses())


async def test_public_repo_never_gets_runners(harness: Harness, pool: RunnerPool) -> None:
    register(harness, "runner-1")
    harness.gateway.private = False

    await tick(harness, pool)

    assert runner_starts(harness) == []
    assert {s.reason for s in pool.statuses()} == {PUBLIC_REPO_PROBLEM}


async def test_unreadable_visibility_fails_closed_and_is_retried(
    harness: Harness, pool: RunnerPool
) -> None:
    register(harness, "runner-1")
    harness.gateway.failures["repo_is_private"] = RuntimeError("down")

    await tick(harness, pool)
    assert runner_starts(harness) == []

    del harness.gateway.failures["repo_is_private"]
    await tick(harness, pool)
    assert len(runner_starts(harness)) == 1


async def test_repo_turning_public_stops_runners_after_the_cache(
    harness: Harness, pool: RunnerPool, clock: Clock
) -> None:
    register(harness, "runner-1")
    await tick(harness, pool)
    harness.gateway.private = False
    clock.now += ELIGIBILITY_TTL_S + 1

    await tick(harness, pool)

    assert harness.runtime.running == {}


async def test_runners_above_the_count_are_removed(harness: Harness, pool: RunnerPool) -> None:
    register(harness, "runner-1")
    register(harness, "runner-2")
    await tick(harness, pool)
    harness.runtime.script(argv_starts_with(*BUSY_ARGV), NOT_BUSY)
    harness.settings = harness.settings.model_copy(
        update={"ci": SelfHostedCi(enabled=True, runners=1)}
    )

    await tick(harness, pool)

    assert [h.name for h in harness.runtime.running.values()] == ["skeleton-crew-runner-1"]


async def test_kill_stops_runners_and_the_loop_keeps_them_down(
    harness: Harness, waiting_task: int
) -> None:
    register(harness, "runner-1")
    pool = RunnerPool(harness.deps())
    controller = RunController(harness.deps(), pool)
    await tick(harness, pool)
    assert harness.runtime.running
    harness.settings = harness.settings.model_copy(update={"kill_switch": True})
    await harness.save_settings()

    await controller.kill()

    assert harness.runtime.running == {}
    await tick(harness, pool)
    assert harness.runtime.running == {}


async def test_preflight_reports_runner_problems(harness: Harness) -> None:
    harness.gateway.private = False

    problems = (await preflight(harness.deps(), harness.settings, harness.gateway)).problems

    assert PUBLIC_REPO_PROBLEM in problems
    assert "self-hosted CI is on, but no runner is registered" in problems


async def test_preflight_ignores_runners_when_off(harness: Harness) -> None:
    harness.settings = harness.settings.model_copy(update={"ci": SelfHostedCi()})
    harness.gateway.private = False

    problems = (await preflight(harness.deps(), harness.settings, harness.gateway)).problems

    assert PUBLIC_REPO_PROBLEM not in problems


async def test_skeleton_ci_on_github_starts_runners_without_a_task(
    harness: Harness, clock: Clock
) -> None:
    register(harness, "runner-1")
    harness.gateway.skeleton_ci = True

    states = await tick(harness, RunnerPool(harness.deps(), monotonic=clock))

    assert states["runner-1"] is RunnerStatus.STARTING
    assert len(runner_starts(harness)) == 1


async def test_github_is_not_asked_while_a_task_waits(harness: Harness, pool: RunnerPool) -> None:
    register(harness, "runner-1")

    await tick(harness, pool)

    assert harness.gateway.skeleton_ci_reads == 0


async def test_github_demand_is_cached(harness: Harness, clock: Clock) -> None:
    register(harness, "runner-1")
    harness.gateway.skeleton_ci = True
    pool = RunnerPool(harness.deps(), monotonic=clock)
    await tick(harness, pool)
    harness.gateway.skeleton_ci = False
    harness.runtime.script(argv_starts_with(*BUSY_ARGV), NOT_BUSY)

    await tick(harness, pool)
    assert harness.runtime.running
    clock.now += CI_DEMAND_TTL_S + 1
    await tick(harness, pool)

    assert harness.gateway.skeleton_ci_reads == 2
    assert harness.runtime.running == {}


async def test_unreadable_github_demand_fails_closed_and_is_retried(
    harness: Harness, clock: Clock
) -> None:
    register(harness, "runner-1")
    harness.gateway.skeleton_ci = True
    harness.gateway.failures["skeleton_ci_active"] = RuntimeError("down")
    pool = RunnerPool(harness.deps(), monotonic=clock)

    await tick(harness, pool)
    assert runner_starts(harness) == []

    del harness.gateway.failures["skeleton_ci_active"]
    await tick(harness, pool)
    assert len(runner_starts(harness)) == 1


async def test_busy_runner_finishes_its_job_when_demand_ends(
    harness: Harness, pool: RunnerPool, waiting_task: int
) -> None:
    register(harness, "runner-1")
    register(harness, "runner-2")
    await tick(harness, pool)
    harness.runtime.script(argv_starts_with(*BUSY_ARGV), ok(), once=True)
    harness.runtime.script(argv_starts_with(*BUSY_ARGV), NOT_BUSY)
    await set_state(harness, waiting_task, TaskState.REVIEWING)

    await tick(harness, pool)

    assert [h.name for h in harness.runtime.running.values()] == ["skeleton-crew-runner-1"]
    assert pool.statuses()[0].status is RunnerStatus.BUSY
    assert pool.statuses()[0].reason == DRAINING_REASON
    assert pool.statuses()[1].status is RunnerStatus.STANDBY

    await tick(harness, pool)

    assert harness.runtime.running == {}
    assert len(runner_starts(harness)) == 2


async def test_kill_switch_removes_a_busy_runner(harness: Harness, pool: RunnerPool) -> None:
    register(harness, "runner-1")
    await tick(harness, pool)
    harness.runtime.script(argv_starts_with(*BUSY_ARGV), ok())
    harness.settings = harness.settings.model_copy(update={"kill_switch": True})

    await tick(harness, pool)

    assert harness.runtime.running == {}
