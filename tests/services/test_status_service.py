from factories import seed_refine, seed_run, seed_task
from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.domain.settings import AppSettings
from skeleton_crew.domain.task_state import RefineState, TaskState
from skeleton_crew.services.status_service import crew_status
from skeleton_crew.store.settings_store import save_settings


async def test_empty_crew_has_defaults_and_nothing_running(session: AsyncSession) -> None:
    status = await crew_status(session)
    assert status.settings == AppSettings()
    assert status.last_run is None
    assert (status.last_run_states, status.last_run_blocked) == ([], 0)
    assert (status.active_tasks, status.active_refines) == ([], [])


async def test_status_summarises_the_newest_run_and_lists_active_work(
    session: AsyncSession,
) -> None:
    await save_settings(session, AppSettings(dry_run=False))
    old = await seed_run(session)
    stale = await seed_task(session, old.id, 1, state=TaskState.CODING)
    run = await seed_run(session)
    await seed_task(session, run.id, 2, state=TaskState.DONE, blockers=["worker: x"])
    await seed_task(session, run.id, 3, state=TaskState.DONE)
    queued = await seed_task(session, run.id, 4)
    refining = await seed_refine(session, run.id, 5, state=RefineState.REFINING)
    await seed_refine(session, run.id, 6, state=RefineState.DONE)

    status = await crew_status(session)

    assert status.settings.dry_run is False
    assert status.last_run == run
    assert status.last_run_states == [(TaskState.DONE, 2), (TaskState.QUEUED, 1)]
    assert status.last_run_blocked == 1
    assert [task.id for task in status.active_tasks] == [stale.id, queued.id]
    assert [task.id for task in status.active_refines] == [refining.id]
