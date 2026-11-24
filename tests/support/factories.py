from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.domain.task_state import RunTrigger, TaskKind
from skeleton_crew.models.refine_task import RefineTask
from skeleton_crew.models.run import Run
from skeleton_crew.models.worker_task import WorkerTask
from skeleton_crew.store import runs_store


async def seed_run(
    session: AsyncSession, trigger: RunTrigger = RunTrigger.MANUAL, *, dry_run: bool = False
) -> Run:
    return await runs_store.create_run(session, trigger, dry_run=dry_run)


async def seed_task(
    session: AsyncSession,
    run_id: int,
    number: int = 1,
    title: str = "t",
    kind: TaskKind = TaskKind.ISSUE,
    **fields: object,
) -> WorkerTask:
    task = await runs_store.add_worker_task(session, run_id, number, title, kind)
    return await runs_store.update_worker_task(session, task.id, **fields) if fields else task


async def seed_refine(
    session: AsyncSession, run_id: int, number: int = 1, **fields: object
) -> RefineTask:
    task = await runs_store.add_refine_task(session, run_id, number)
    return await runs_store.update_refine_task(session, task.id, **fields) if fields else task
