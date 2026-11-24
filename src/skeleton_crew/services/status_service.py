from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.domain.settings import AppSettings
from skeleton_crew.domain.task_state import TaskState
from skeleton_crew.models.refine_task import RefineTask
from skeleton_crew.models.run import Run
from skeleton_crew.models.worker_task import WorkerTask
from skeleton_crew.store import runs_store, settings_store


@dataclass(frozen=True)
class CrewStatus:
    settings: AppSettings
    last_run: Run | None
    last_run_states: list[tuple[TaskState, int]]
    last_run_blocked: int
    active_tasks: list[WorkerTask]
    active_refines: list[RefineTask]


async def crew_status(session: AsyncSession) -> CrewStatus:
    latest = await runs_store.runs_with_blocked_counts(session, limit=1, offset=0)
    last_run, blocked = latest[0] if latest else (None, 0)
    states = await runs_store.task_state_counts(session, last_run.id) if last_run else []
    return CrewStatus(
        settings=await settings_store.load_settings(session),
        last_run=last_run,
        last_run_states=states,
        last_run_blocked=blocked,
        active_tasks=await runs_store.non_terminal_worker_tasks(session),
        active_refines=await runs_store.non_terminal_refine_tasks(session),
    )
