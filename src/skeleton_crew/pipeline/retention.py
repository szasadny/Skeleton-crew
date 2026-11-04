import asyncio
import logging
from datetime import datetime, timedelta

from skeleton_crew.domain.task_keys import refine_key, selector_key, worker_key
from skeleton_crew.pipeline.deps import PipelineDeps
from skeleton_crew.runtime.workspace import remove_task_dirs
from skeleton_crew.store import runs_store

log = logging.getLogger(__name__)


async def expired_task_keys(deps: PipelineDeps, before: datetime) -> list[str]:
    async with deps.sessionmaker() as session:
        workers = await runs_store.terminal_worker_task_ids_before(session, before)
        refines = await runs_store.terminal_refine_task_ids_before(session, before)
        runs = await runs_store.finished_run_ids_before(session, before)
    return [
        *map(worker_key, workers),
        *map(refine_key, refines),
        *map(selector_key, runs),
    ]


async def prune_task_dirs(deps: PipelineDeps, now: datetime, retention_days: int) -> None:
    try:
        keys = await expired_task_keys(deps, now - timedelta(days=retention_days))
        removed = await asyncio.to_thread(remove_task_dirs, deps.config.tasks_dir, keys)
    except Exception:
        log.exception("task directory cleanup failed")
        return
    if removed:
        log.info("removed %d expired task directories", len(removed))
