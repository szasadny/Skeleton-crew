from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.domain.task_state import RefineState, RunState, TaskKind, TaskState
from skeleton_crew.store import runs_store


async def test_run_lifecycle(session: AsyncSession) -> None:
    run = await runs_store.create_run(session, "schedule", dry_run=False)
    assert run.state is RunState.RUNNING
    assert run.finished_at is None
    finished = await runs_store.finish_run(session, run.id, RunState.COMPLETED, note="ok")
    assert finished.finished_at is not None
    assert finished.finished_at >= finished.started_at
    assert (await runs_store.get_run(session, run.id)) is finished


async def test_finish_run_requires_final_state_and_existing_run(session: AsyncSession) -> None:
    run = await runs_store.create_run(session, "manual", dry_run=True)
    with pytest.raises(ValueError, match="final state"):
        await runs_store.finish_run(session, run.id, RunState.RUNNING)
    with pytest.raises(LookupError):
        await runs_store.finish_run(session, 999, RunState.FAILED)


async def test_unknown_trigger_is_rejected(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="trigger"):
        await runs_store.create_run(session, "webhook", dry_run=True)  # type: ignore[arg-type]


async def test_list_runs_newest_first(session: AsyncSession) -> None:
    ids = [(await runs_store.create_run(session, "manual", dry_run=True)).id for _ in range(3)]
    listed = await runs_store.list_runs(session, limit=2, offset=1)
    assert [r.id for r in listed] == [ids[1], ids[0]]


async def test_worker_task_defaults_and_update(session: AsyncSession) -> None:
    run = await runs_store.create_run(session, "manual", dry_run=True)
    task = await runs_store.add_worker_task(session, run.id, 12, "Fix login")
    assert (task.state, task.ci_rounds, task.cost_usd) == (TaskState.QUEUED, 0, 0.0)
    updated = await runs_store.update_worker_task(
        session, task.id, state=TaskState.CODING, branch="skeleton/12-fix-login", ci_rounds=1
    )
    assert (updated.state, updated.branch, updated.ci_rounds) == (
        TaskState.CODING,
        "skeleton/12-fix-login",
        1,
    )
    assert updated.updated_at >= updated.created_at
    assert await runs_store.list_tasks(session, run.id) == [updated]


@pytest.mark.parametrize("field", ["id", "run_id", "created_at", "nonexistent"])
async def test_update_worker_task_rejects_protected_or_unknown_fields(
    session: AsyncSession, field: str
) -> None:
    run = await runs_store.create_run(session, "manual", dry_run=True)
    task = await runs_store.add_worker_task(session, run.id, 1, "t")
    with pytest.raises(ValueError, match="cannot update"):
        await runs_store.update_worker_task(session, task.id, **{field: 1})


async def test_update_missing_task_raises(session: AsyncSession) -> None:
    with pytest.raises(LookupError):
        await runs_store.update_worker_task(session, 999, state=TaskState.FAILED)
    with pytest.raises(LookupError):
        await runs_store.update_refine_task(session, 999, state=RefineState.FAILED)


async def test_refine_task_update_and_listing(session: AsyncSession) -> None:
    run = await runs_store.create_run(session, "manual", dry_run=True)
    task = await runs_store.add_refine_task(session, run.id, 5)
    await runs_store.update_refine_task(session, task.id, state=RefineState.FAILED, error="boom")
    (listed,) = await runs_store.list_refine_tasks(session, run.id)
    assert (listed.state, listed.error) == (RefineState.FAILED, "boom")
    with pytest.raises(ValueError, match="cannot update"):
        await runs_store.update_refine_task(session, task.id, run_id=2)


async def test_non_terminal_queries_skip_finished_tasks(session: AsyncSession) -> None:
    run = await runs_store.create_run(session, "manual", dry_run=True)
    active = await runs_store.add_worker_task(session, run.id, 1, "a")
    await runs_store.update_worker_task(session, active.id, state=TaskState.AWAITING_CI)
    for state in (TaskState.DONE, TaskState.ESCALATED, TaskState.FAILED, TaskState.ABORTED):
        done = await runs_store.add_worker_task(session, run.id, 2, "b")
        await runs_store.update_worker_task(session, done.id, state=state)
    refining = await runs_store.add_refine_task(session, run.id, 3)
    await runs_store.update_refine_task(session, refining.id, state=RefineState.REFINING)
    queued = await runs_store.add_refine_task(session, run.id, 4)
    finished = await runs_store.add_refine_task(session, run.id, 5)
    await runs_store.update_refine_task(session, finished.id, state=RefineState.DONE)
    assert [t.id for t in await runs_store.non_terminal_worker_tasks(session)] == [active.id]
    assert [t.id for t in await runs_store.non_terminal_refine_tasks(session)] == [
        refining.id,
        queued.id,
    ]


async def test_count_tasks_since_only_counts_newer_tasks(session: AsyncSession) -> None:
    run = await runs_store.create_run(session, "manual", dry_run=True)
    old = await runs_store.add_worker_task(session, run.id, 1, "Old")
    await runs_store.add_worker_task(session, run.id, 2, "New")
    refine = await runs_store.add_refine_task(session, run.id, 3)
    old.created_at -= timedelta(days=1)
    await session.flush()
    since = refine.created_at - timedelta(hours=1)
    assert await runs_store.count_worker_tasks_since(session, since) == 1
    assert await runs_store.count_refine_tasks_since(session, since) == 1
    later = refine.created_at + timedelta(seconds=1)
    assert await runs_store.count_refine_tasks_since(session, later) == 0


async def test_running_runs_lists_only_running(session: AsyncSession) -> None:
    running = await runs_store.create_run(session, "manual", dry_run=True)
    finished = await runs_store.create_run(session, "schedule", dry_run=True)
    await runs_store.finish_run(session, finished.id, RunState.COMPLETED)
    assert [r.id for r in await runs_store.running_runs(session)] == [running.id]


async def test_counts_are_per_task_kind(session: AsyncSession) -> None:
    run = await runs_store.create_run(session, "manual", dry_run=False)
    issue = await runs_store.add_worker_task(session, run.id, 1, "t")
    await runs_store.add_worker_task(session, run.id, 2, "t", TaskKind.PR_UPDATE)
    fix = await runs_store.add_worker_task(session, run.id, 3, "t", TaskKind.PR_UPDATE)
    await runs_store.update_worker_task(session, fix.id, kind=TaskKind.PR_FIX)
    since = issue.created_at - timedelta(hours=1)
    assert await runs_store.count_worker_tasks_since(session, since) == 1
    assert await runs_store.count_worker_tasks_since(session, since, TaskKind.PR_FIX) == 1
    assert await runs_store.count_worker_tasks_since(session, since, TaskKind.PR_UPDATE) == 1


async def test_last_pushed_task_ignores_dry_runs_and_unpushed_tasks(session: AsyncSession) -> None:
    live = await runs_store.create_run(session, "manual", dry_run=False)
    dry = await runs_store.create_run(session, "manual", dry_run=True)
    pushed = await runs_store.add_worker_task(session, live.id, 4, "t")
    await runs_store.update_worker_task(session, pushed.id, head_sha="a" * 40)
    rehearsed = await runs_store.add_worker_task(session, dry.id, 4, "t")
    await runs_store.update_worker_task(session, rehearsed.id, head_sha="b" * 40)
    await runs_store.add_worker_task(session, live.id, 4, "t")
    other = await runs_store.add_worker_task(session, live.id, 5, "t")
    await runs_store.update_worker_task(session, other.id, head_sha="c" * 40)

    last = await runs_store.last_pushed_task(session, 4)

    assert last is not None
    assert last.id == pushed.id
    assert await runs_store.last_pushed_task(session, 6) is None
