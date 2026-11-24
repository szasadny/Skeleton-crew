from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ColumnElement, Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from skeleton_crew.core.clock import utc_now
from skeleton_crew.domain.task_state import (
    QUOTA_FREE_TASK_STATES,
    TERMINAL_REFINE_STATES,
    TERMINAL_TASK_STATES,
    RefineState,
    RunState,
    RunTrigger,
    TaskKind,
    TaskState,
)
from skeleton_crew.models.refine_task import RefineTask
from skeleton_crew.models.run import Run
from skeleton_crew.models.worker_task import WorkerTask


@dataclass(frozen=True)
class _TaskTable:
    name: str
    fields: frozenset[str]
    terminal: frozenset[StrEnum]


_TABLES: dict[type[WorkerTask | RefineTask], _TaskTable] = {
    WorkerTask: _TaskTable(
        "worker task",
        frozenset(
            {
                "issue_title",
                "state",
                "kind",
                "branch",
                "pr_number",
                "base_sha",
                "head_sha",
                "session_id",
                "container_id",
                "check_rounds",
                "ci_rounds",
                "review_rounds",
                "policy_rounds",
                "cost_usd",
                "note",
                "blockers",
            }
        ),
        frozenset(TERMINAL_TASK_STATES),
    ),
    RefineTask: _TaskTable(
        "refine task",
        frozenset({"state", "session_id", "cost_usd", "error"}),
        frozenset(TERMINAL_REFINE_STATES),
    ),
}


@dataclass(frozen=True)
class RunWithTasks:
    run: Run
    tasks: list[WorkerTask]
    refines: list[RefineTask]


async def _count(session: AsyncSession, query: Select[int]) -> int:
    return int(await session.scalar(query) or 0)


async def _update[T: (WorkerTask, RefineTask)](
    session: AsyncSession, model: type[T], task_id: int, fields: dict[str, object]
) -> T:
    table = _TABLES[model]
    task = await session.get(model, task_id)
    if task is None:
        raise LookupError(f"{table.name} {task_id} not found")
    unknown = set(fields) - table.fields
    if unknown:
        raise ValueError(f"cannot update fields: {sorted(unknown)}")
    for name, value in fields.items():
        setattr(task, name, value)
    await session.flush()
    return task


async def _by_run[T: (WorkerTask, RefineTask)](
    session: AsyncSession, model: type[T], run_id: int
) -> list[T]:
    query = select(model).where(model.run_id == run_id).order_by(model.id)
    return list(await session.scalars(query))


async def _non_terminal[T: (WorkerTask, RefineTask)](
    session: AsyncSession, model: type[T]
) -> list[T]:
    query = select(model).where(model.state.not_in(_TABLES[model].terminal)).order_by(model.id)
    return list(await session.scalars(query))


async def _expired_ids(
    session: AsyncSession,
    id_column: InstrumentedAttribute[int],
    ids: Collection[int],
    *conditions: ColumnElement[bool],
) -> list[int]:
    query = select(id_column).where(id_column.in_(ids), *conditions).order_by(id_column)
    return list(await session.scalars(query))


async def _expired_task_ids[T: (WorkerTask, RefineTask)](
    session: AsyncSession, model: type[T], ids: Collection[int], before: datetime
) -> list[int]:
    terminal = model.state.in_(_TABLES[model].terminal)
    return await _expired_ids(session, model.id, ids, terminal, model.updated_at < before)


async def create_run(session: AsyncSession, trigger: RunTrigger, dry_run: bool) -> Run:
    run = Run(trigger=trigger, dry_run=dry_run, state=RunState.RUNNING)
    session.add(run)
    await session.flush()
    return run


async def running_runs(session: AsyncSession) -> list[Run]:
    query = select(Run).where(Run.state == RunState.RUNNING).order_by(Run.id)
    return list(await session.scalars(query))


async def get_run(session: AsyncSession, run_id: int) -> Run | None:
    return await session.get(Run, run_id)


async def run_with_tasks(session: AsyncSession, run_id: int) -> RunWithTasks | None:
    run = await get_run(session, run_id)
    if run is None:
        return None
    return RunWithTasks(
        run, await list_tasks(session, run_id), await list_refine_tasks(session, run_id)
    )


async def finish_run(
    session: AsyncSession, run_id: int, state: RunState, note: str | None = None
) -> Run:
    if state is RunState.RUNNING:
        raise ValueError("a finished run needs a final state")
    run = await get_run(session, run_id)
    if run is None:
        raise LookupError(f"run {run_id} not found")
    run.state = state
    run.finished_at = utc_now()
    run.note = note
    await session.flush()
    return run


async def list_runs(session: AsyncSession, limit: int, offset: int) -> list[Run]:
    query = select(Run).order_by(Run.id.desc()).limit(limit).offset(offset)
    return list(await session.scalars(query))


async def runs_with_blocked_counts(
    session: AsyncSession, limit: int, offset: int
) -> list[tuple[Run, int]]:
    runs = await list_runs(session, limit, offset)
    counts = await count_tasks_with_blockers(session, [run.id for run in runs])
    return [(run, counts.get(run.id, 0)) for run in runs]


async def add_worker_task(
    session: AsyncSession,
    run_id: int,
    issue_number: int,
    issue_title: str,
    kind: TaskKind = TaskKind.ISSUE,
) -> WorkerTask:
    task = WorkerTask(
        run_id=run_id,
        issue_number=issue_number,
        issue_title=issue_title,
        state=TaskState.QUEUED,
        kind=kind,
    )
    session.add(task)
    await session.flush()
    return task


async def get_worker_task(session: AsyncSession, task_id: int) -> WorkerTask | None:
    return await session.get(WorkerTask, task_id)


async def update_worker_task(session: AsyncSession, task_id: int, **fields: object) -> WorkerTask:
    return await _update(session, WorkerTask, task_id, fields)


async def list_tasks(session: AsyncSession, run_id: int) -> list[WorkerTask]:
    return await _by_run(session, WorkerTask, run_id)


async def task_state_counts(session: AsyncSession, run_id: int) -> list[tuple[TaskState, int]]:
    query = (
        select(WorkerTask.state, func.count())
        .where(WorkerTask.run_id == run_id)
        .group_by(WorkerTask.state)
        .order_by(WorkerTask.state)
    )
    return [(state, count) for state, count in (await session.execute(query)).all()]


def _has_blockers() -> ColumnElement[bool]:
    return and_(WorkerTask.blockers.is_not(None), WorkerTask.blockers != [])


async def count_tasks_with_blockers(
    session: AsyncSession, run_ids: Collection[int]
) -> dict[int, int]:
    query = (
        select(WorkerTask.run_id, func.count())
        .where(WorkerTask.run_id.in_(run_ids), _has_blockers())
        .group_by(WorkerTask.run_id)
    )
    return {run_id: count for run_id, count in (await session.execute(query)).all()}


async def list_tasks_with_blockers(
    session: AsyncSession, limit: int | None = None, offset: int = 0
) -> list[WorkerTask]:
    query = select(WorkerTask).where(_has_blockers()).order_by(WorkerTask.id.desc()).offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return list(await session.scalars(query))


async def add_refine_task(session: AsyncSession, run_id: int, issue_number: int) -> RefineTask:
    task = RefineTask(run_id=run_id, issue_number=issue_number, state=RefineState.QUEUED)
    session.add(task)
    await session.flush()
    return task


async def get_refine_task(session: AsyncSession, task_id: int) -> RefineTask | None:
    return await session.get(RefineTask, task_id)


async def update_refine_task(session: AsyncSession, task_id: int, **fields: object) -> RefineTask:
    return await _update(session, RefineTask, task_id, fields)


async def list_refine_tasks(session: AsyncSession, run_id: int) -> list[RefineTask]:
    return await _by_run(session, RefineTask, run_id)


async def non_terminal_worker_tasks(session: AsyncSession) -> list[WorkerTask]:
    return await _non_terminal(session, WorkerTask)


async def non_terminal_refine_tasks(session: AsyncSession) -> list[RefineTask]:
    return await _non_terminal(session, RefineTask)


async def count_worker_tasks_since(
    session: AsyncSession, since: datetime, kind: TaskKind = TaskKind.ISSUE
) -> int:
    query = (
        select(func.count())
        .select_from(WorkerTask)
        .where(
            WorkerTask.created_at >= since,
            WorkerTask.kind == kind,
            WorkerTask.state.not_in(QUOTA_FREE_TASK_STATES),
        )
    )
    return await _count(session, query)


async def awaiting_ci_count(session: AsyncSession) -> int:
    query = (
        select(func.count())
        .select_from(WorkerTask)
        .join(Run, Run.id == WorkerTask.run_id)
        .where(
            WorkerTask.state == TaskState.AWAITING_CI,
            Run.state == RunState.RUNNING,
            Run.dry_run.is_(False),
        )
    )
    return await _count(session, query)


async def last_pushed_task(session: AsyncSession, issue_number: int) -> WorkerTask | None:
    query = (
        select(WorkerTask)
        .join(Run, Run.id == WorkerTask.run_id)
        .where(
            WorkerTask.issue_number == issue_number,
            WorkerTask.head_sha.is_not(None),
            Run.dry_run.is_(False),
        )
        .order_by(WorkerTask.id.desc())
        .limit(1)
    )
    return await session.scalar(query)


async def count_refine_tasks_since(session: AsyncSession, since: datetime) -> int:
    query = select(func.count()).select_from(RefineTask).where(RefineTask.created_at >= since)
    return await _count(session, query)


async def expired_worker_task_ids(
    session: AsyncSession, ids: Collection[int], before: datetime
) -> list[int]:
    return await _expired_task_ids(session, WorkerTask, ids, before)


async def expired_refine_task_ids(
    session: AsyncSession, ids: Collection[int], before: datetime
) -> list[int]:
    return await _expired_task_ids(session, RefineTask, ids, before)


async def expired_run_ids(
    session: AsyncSession, ids: Collection[int], before: datetime
) -> list[int]:
    finished = and_(Run.state != RunState.RUNNING, Run.finished_at < before)
    return await _expired_ids(session, Run.id, ids, finished)
