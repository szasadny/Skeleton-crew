from datetime import timedelta

import pytest
from factories import seed_refine, seed_run, seed_task
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.core.clock import utc_now
from skeleton_crew.domain.task_state import RefineState, RunState, RunTrigger, TaskKind, TaskState
from skeleton_crew.store import runs_store


async def test_run_lifecycle(session: AsyncSession) -> None:
    run = await runs_store.create_run(session, RunTrigger.SCHEDULE, dry_run=False)
    assert run.state is RunState.RUNNING
    assert run.finished_at is None
    finished = await runs_store.finish_run(session, run.id, RunState.COMPLETED, note="ok")
    assert finished.finished_at is not None
    assert finished.finished_at >= finished.started_at
    assert (await runs_store.get_run(session, run.id)) is finished


async def test_finish_run_requires_final_state_and_existing_run(session: AsyncSession) -> None:
    run = await runs_store.create_run(session, RunTrigger.MANUAL, dry_run=True)
    with pytest.raises(ValueError, match="final state"):
        await runs_store.finish_run(session, run.id, RunState.RUNNING)
    with pytest.raises(LookupError):
        await runs_store.finish_run(session, 999, RunState.FAILED)


async def test_unknown_trigger_is_rejected(session: AsyncSession) -> None:
    with pytest.raises(StatementError, match="not a valid RunTrigger"):
        await runs_store.create_run(session, "webhook", dry_run=True)  # type: ignore[arg-type]


async def test_list_runs_newest_first(session: AsyncSession) -> None:
    ids = [
        (await runs_store.create_run(session, RunTrigger.MANUAL, dry_run=True)).id for _ in range(3)
    ]
    listed = await runs_store.list_runs(session, limit=2, offset=1)
    assert [r.id for r in listed] == [ids[1], ids[0]]


async def test_worker_task_defaults_and_update(session: AsyncSession) -> None:
    run = await runs_store.create_run(session, RunTrigger.MANUAL, dry_run=True)
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
    run = await runs_store.create_run(session, RunTrigger.MANUAL, dry_run=True)
    task = await runs_store.add_worker_task(session, run.id, 1, "t")
    with pytest.raises(ValueError, match="cannot update"):
        await runs_store.update_worker_task(session, task.id, **{field: 1})


async def test_update_missing_task_raises(session: AsyncSession) -> None:
    with pytest.raises(LookupError):
        await runs_store.update_worker_task(session, 999, state=TaskState.FAILED)
    with pytest.raises(LookupError):
        await runs_store.update_refine_task(session, 999, state=RefineState.FAILED)


async def test_refine_task_update_and_listing(session: AsyncSession) -> None:
    run = await runs_store.create_run(session, RunTrigger.MANUAL, dry_run=True)
    task = await runs_store.add_refine_task(session, run.id, 5)
    await runs_store.update_refine_task(session, task.id, state=RefineState.FAILED, error="boom")
    (listed,) = await runs_store.list_refine_tasks(session, run.id)
    assert (listed.state, listed.error) == (RefineState.FAILED, "boom")
    with pytest.raises(ValueError, match="cannot update"):
        await runs_store.update_refine_task(session, task.id, run_id=2)


async def test_non_terminal_queries_skip_finished_tasks(session: AsyncSession) -> None:
    run = await seed_run(session, dry_run=True)
    active = await seed_task(session, run.id, 1, state=TaskState.AWAITING_CI)
    for state in (TaskState.DONE, TaskState.ESCALATED, TaskState.FAILED, TaskState.ABORTED):
        await seed_task(session, run.id, 2, state=state)
    refining = await seed_refine(session, run.id, 3, state=RefineState.REFINING)
    queued = await seed_refine(session, run.id, 4)
    await seed_refine(session, run.id, 5, state=RefineState.DONE)
    assert [t.id for t in await runs_store.non_terminal_worker_tasks(session)] == [active.id]
    assert [t.id for t in await runs_store.non_terminal_refine_tasks(session)] == [
        refining.id,
        queued.id,
    ]


async def test_count_tasks_since_only_counts_newer_tasks(session: AsyncSession) -> None:
    run = await runs_store.create_run(session, RunTrigger.MANUAL, dry_run=True)
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
    running = await runs_store.create_run(session, RunTrigger.MANUAL, dry_run=True)
    finished = await runs_store.create_run(session, RunTrigger.SCHEDULE, dry_run=True)
    await runs_store.finish_run(session, finished.id, RunState.COMPLETED)
    assert [r.id for r in await runs_store.running_runs(session)] == [running.id]


async def test_counts_are_per_task_kind(session: AsyncSession) -> None:
    run = await runs_store.create_run(session, RunTrigger.MANUAL, dry_run=False)
    issue = await runs_store.add_worker_task(session, run.id, 1, "t")
    await runs_store.add_worker_task(session, run.id, 2, "t", TaskKind.PR_FIX)
    fix = await runs_store.add_worker_task(session, run.id, 3, "t")
    await runs_store.update_worker_task(session, fix.id, kind=TaskKind.PR_FIX)
    since = issue.created_at - timedelta(hours=1)
    assert await runs_store.count_worker_tasks_since(session, since) == 1
    assert await runs_store.count_worker_tasks_since(session, since, TaskKind.PR_FIX) == 2


@pytest.mark.parametrize("kind", [TaskKind.ISSUE, TaskKind.PR_FIX])
async def test_count_skips_aborted_tasks_but_counts_every_other_state(
    session: AsyncSession, kind: TaskKind
) -> None:
    run = await seed_run(session)
    other = TaskKind.PR_FIX if kind is TaskKind.ISSUE else TaskKind.ISSUE
    counted = [state for state in TaskState if state is not TaskState.ABORTED]
    for state in [*counted, TaskState.ABORTED, TaskState.ABORTED]:
        await seed_task(session, run.id, kind=kind, state=state)
    await seed_task(session, run.id, 2, kind=other, state=TaskState.DONE)
    since = run.started_at - timedelta(hours=1)
    assert await runs_store.count_worker_tasks_since(session, since, kind) == len(counted)
    assert await runs_store.count_worker_tasks_since(session, since, other) == 1


async def test_count_drops_a_task_once_it_is_aborted(
    session: AsyncSession,
) -> None:
    run = await runs_store.create_run(session, RunTrigger.MANUAL, dry_run=False)
    task = await runs_store.add_worker_task(session, run.id, 1, "t")
    since = run.started_at - timedelta(hours=1)
    assert await runs_store.count_worker_tasks_since(session, since) == 1
    await runs_store.update_worker_task(session, task.id, state=TaskState.ABORTED)
    assert await runs_store.count_worker_tasks_since(session, since) == 0


async def test_last_pushed_task_ignores_dry_runs_and_unpushed_tasks(session: AsyncSession) -> None:
    live = await seed_run(session)
    dry = await seed_run(session, dry_run=True)
    pushed = await seed_task(session, live.id, 4, head_sha="a" * 40)
    await seed_task(session, dry.id, 4, head_sha="b" * 40)
    await seed_task(session, live.id, 4)
    await seed_task(session, live.id, 5, head_sha="c" * 40)

    last = await runs_store.last_pushed_task(session, 4)

    assert last is not None
    assert last.id == pushed.id
    assert await runs_store.last_pushed_task(session, 6) is None


async def test_last_pushed_tasks_keep_the_newest_live_push_per_issue(
    session: AsyncSession,
) -> None:
    live = await seed_run(session)
    dry = await seed_run(session, dry_run=True)
    await seed_task(session, live.id, 4, head_sha="a" * 40)
    newest = await seed_task(session, live.id, 4, head_sha="d" * 40)
    await seed_task(session, dry.id, 4, head_sha="b" * 40)
    await seed_task(session, live.id, 4)
    other = await seed_task(session, live.id, 5, head_sha="c" * 40)
    await seed_task(session, dry.id, 7, head_sha="e" * 40)
    await seed_task(session, live.id, 8, head_sha="f" * 40)

    found = await runs_store.last_pushed_tasks(session, [4, 5, 6, 7])

    assert {number: task.id for number, task in found.items()} == {4: newest.id, 5: other.id}
    assert await runs_store.last_pushed_tasks(session, []) == {}


async def test_awaiting_ci_count_only_counts_live_running_runs(session: AsyncSession) -> None:
    live = await seed_run(session)
    dry = await seed_run(session, dry_run=True)
    finished = await seed_run(session)
    for run_id in (live.id, live.id, dry.id, finished.id):
        await seed_task(session, run_id, state=TaskState.AWAITING_CI)
    await seed_task(session, live.id, 2, state=TaskState.CODING)
    await runs_store.finish_run(session, finished.id, RunState.FAILED)

    assert await runs_store.awaiting_ci_count(session) == 2


async def test_expired_ids_only_cover_listed_old_terminal_rows(session: AsyncSession) -> None:
    run = await seed_run(session)
    live = await seed_run(session)
    done = await seed_task(session, run.id, 1, state=TaskState.DONE)
    await seed_task(session, run.id, 5, state=TaskState.DONE)
    queued = await seed_task(session, run.id, 2)
    refined = await seed_refine(session, run.id, 3, state=RefineState.DONE)
    pending = await seed_refine(session, run.id, 4)
    await runs_store.finish_run(session, run.id, RunState.COMPLETED)
    later = utc_now() + timedelta(seconds=1)
    earlier = done.updated_at - timedelta(seconds=1)
    workers, refines, runs = [done.id, queued.id, 999], [refined.id, pending.id], [run.id, live.id]

    assert await runs_store.expired_worker_task_ids(session, workers, later) == [done.id]
    assert await runs_store.expired_refine_task_ids(session, refines, later) == [refined.id]
    assert await runs_store.expired_run_ids(session, runs, later) == [run.id]
    assert await runs_store.expired_worker_task_ids(session, workers, earlier) == []
    assert await runs_store.expired_refine_task_ids(session, refines, earlier) == []
    assert await runs_store.expired_run_ids(session, runs, earlier) == []
    assert await runs_store.expired_worker_task_ids(session, [], later) == []


async def test_list_tasks_with_blockers_newest_first_and_paged(session: AsyncSession) -> None:
    run = await seed_run(session, dry_run=True)
    first = await seed_task(session, run.id, 1, blockers=["a"])
    second = await seed_task(session, run.id, 2)
    third = await seed_task(session, run.id, 3, blockers=["b", "c"])
    await seed_task(session, run.id, 4, blockers=[])
    assert second.blockers is None
    assert await runs_store.list_tasks_with_blockers(session) == [third, first]
    assert await runs_store.list_tasks_with_blockers(session, limit=1, offset=1) == [first]


async def test_run_with_tasks_loads_a_run_and_its_tasks(session: AsyncSession) -> None:
    run = await seed_run(session)
    other = await seed_run(session)
    task = await seed_task(session, run.id, 1)
    await seed_task(session, other.id, 2)
    refine = await seed_refine(session, run.id, 3)
    loaded = await runs_store.run_with_tasks(session, run.id)
    assert loaded is not None
    assert (loaded.run, loaded.tasks, loaded.refines) == (run, [task], [refine])
    assert await runs_store.run_with_tasks(session, 999) is None


async def test_task_state_counts_group_one_run_by_state(session: AsyncSession) -> None:
    run = await seed_run(session)
    other = await seed_run(session)
    for state in (TaskState.QUEUED, TaskState.DONE, TaskState.QUEUED, TaskState.CODING):
        await seed_task(session, run.id, state=state)
    await seed_task(session, other.id, state=TaskState.FAILED)
    assert await runs_store.task_state_counts(session, run.id) == [
        (TaskState.CODING, 1),
        (TaskState.DONE, 1),
        (TaskState.QUEUED, 2),
    ]
    assert await runs_store.task_state_counts(session, 999) == []


async def test_runs_with_blocked_counts_pages_newest_first(session: AsyncSession) -> None:
    first, second, third = [await seed_run(session) for _ in range(3)]
    await seed_task(session, first.id, 1, blockers=["a"])
    await seed_task(session, first.id, 2, blockers=["b", "c"])
    await seed_task(session, first.id, 3, blockers=[])
    await seed_task(session, third.id, 4, blockers=["d"])
    assert await runs_store.runs_with_blocked_counts(session, limit=2, offset=1) == [
        (second, 0),
        (first, 2),
    ]
    assert await runs_store.runs_with_blocked_counts(session, limit=1, offset=0) == [(third, 1)]


async def test_get_refine_task(session: AsyncSession) -> None:
    run = await seed_run(session)
    refine = await seed_refine(session, run.id, 3)
    assert await runs_store.get_refine_task(session, refine.id) is refine
    assert await runs_store.get_refine_task(session, 999) is None
