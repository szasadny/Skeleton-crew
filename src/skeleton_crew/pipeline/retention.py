import asyncio
import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta

from skeleton_crew.domain.task_keys import (
    KeyKind,
    parse_task_key,
    refine_key,
    selector_key,
    worker_key,
)
from skeleton_crew.pipeline.deps import PipelineDeps
from skeleton_crew.runtime.workspace import list_task_keys, remove_task_dirs
from skeleton_crew.store import runs_store

log = logging.getLogger(__name__)


def _ids_by_kind(keys: Iterable[str]) -> dict[KeyKind, list[int]]:
    grouped: dict[KeyKind, list[int]] = defaultdict(list)
    for key in keys:
        parsed = parse_task_key(key)
        if parsed is not None:
            grouped[parsed[0]].append(parsed[1])
    return grouped


async def expired_task_keys(
    deps: PipelineDeps, on_disk: Iterable[str], before: datetime
) -> list[str]:
    ids = _ids_by_kind(on_disk)
    async with deps.sessionmaker() as session:
        workers = await runs_store.expired_worker_task_ids(session, ids[KeyKind.WORKER], before)
        refines = await runs_store.expired_refine_task_ids(session, ids[KeyKind.REFINE], before)
        runs = await runs_store.expired_run_ids(session, ids[KeyKind.SELECTOR], before)
    return [
        *map(worker_key, workers),
        *map(refine_key, refines),
        *map(selector_key, runs),
    ]


async def prune_task_dirs(deps: PipelineDeps, now: datetime, retention_days: int) -> None:
    tasks_dir = deps.config.tasks_dir
    try:
        on_disk = await asyncio.to_thread(list_task_keys, tasks_dir)
        keys = await expired_task_keys(deps, on_disk, now - timedelta(days=retention_days))
        removed = await asyncio.to_thread(remove_task_dirs, tasks_dir, keys)
    except Exception:
        log.exception("task directory cleanup failed")
        return
    if removed:
        log.info("removed %d expired task directories", len(removed))
