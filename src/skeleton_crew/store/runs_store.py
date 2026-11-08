from collections.abc import Collection
from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.domain.task_state import (
    TERMINAL_REFINE_STATES,
    TERMINAL_TASK_STATES,
    RefineState,
    RunState,
    TaskKind,
    TaskState,
)
from skeleton_crew.models.refine_task import RefineTask
from skeleton_crew.models.run import Run
from skeleton_crew.models.types import utc_now
from skeleton_crew.models.worker_task import WorkerTask

type RunTrigger = Literal["schedule", "manual"]

_TRIGGERS = frozenset({"schedule", "manual"})
_WORKER_TASK_FIELDS = frozenset(
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
)
_REFINE_TASK_FIELDS = frozenset({"state", "session_id", "cost_usd", "error"})


def _apply(
    target: WorkerTask | RefineTask, allowed: frozenset[str], fields: dict[str, object]
) -> None:
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot update fields: {sorted(unknown)}")
    for name, value in fields.items():
        setattr(target, name, value)


async def create_run(session: AsyncSession, trigger: RunTrigger, dry_run: bool) -> Run:
    if trigger not in _TRIGGERS:
        raise ValueError(f"unknown trigger: {trigger}")
    run = Run(trigger=trigger, dry_run=dry_run, state=RunState.RUNNING)
    session.add(run)
    await session.flush()
    return run


async def running_runs(session: AsyncSession) -> list[Run]:
    query = select(Run).where(Run.state == RunState.RUNNING).order_by(Run.id)
    return list(await session.scalars(query))


async def get_run(session: AsyncSession, run_id: int) -> Run | None:
    return await session.get(Run, run_id)


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
    task = await get_worker_task(session, task_id)
    if task is None:
        raise LookupError(f"worker task {task_id} not found")
    _apply(task, _WORKER_TASK_FIELDS, fields)
    await session.flush()
    return task


async def list_tasks(session: AsyncSession, run_id: int) -> list[WorkerTask]:
    query = select(WorkerTask).where(WorkerTask.run_id == run_id).order_by(WorkerTask.id)
    return list(await session.scalars(query))


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


async def update_refine_task(session: AsyncSession, task_id: int, **fields: object) -> RefineTask:
    task = await session.get(RefineTask, task_id)
    if task is None:
        raise LookupError(f"refine task {task_id} not found")
    _apply(task, _REFINE_TASK_FIELDS, fields)
    await session.flush()
    return task


async def list_refine_tasks(session: AsyncSession, run_id: int) -> list[RefineTask]:
    query = select(RefineTask).where(RefineTask.run_id == run_id).order_by(RefineTask.id)
    return list(await session.scalars(query))


async def non_terminal_worker_tasks(session: AsyncSession) -> list[WorkerTask]:
    query = (
        select(WorkerTask)
        .where(WorkerTask.state.not_in(TERMINAL_TASK_STATES))
        .order_by(WorkerTask.id)
    )
    return list(await session.scalars(query))


async def non_terminal_refine_tasks(session: AsyncSession) -> list[RefineTask]:
    query = (
        select(RefineTask)
        .where(RefineTask.state.not_in(TERMINAL_REFINE_STATES))
        .order_by(RefineTask.id)
    )
    return list(await session.scalars(query))


async def count_worker_tasks_since(
    session: AsyncSession, since: datetime, kind: TaskKind = TaskKind.ISSUE
) -> int:
    query = (
        select(func.count())
        .select_from(WorkerTask)
        .where(WorkerTask.created_at >= since, WorkerTask.kind == kind)
    )
    return int(await session.scalar(query) or 0)


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
    return int(await session.scalar(query) or 0)


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
    return int(await session.scalar(query) or 0)


async def expired_worker_task_ids(
    session: AsyncSession, ids: Collection[int], before: datetime
) -> list[int]:
    query = select(WorkerTask.id).where(
        WorkerTask.id.in_(ids),
        WorkerTask.state.in_(TERMINAL_TASK_STATES),
        WorkerTask.updated_at < before,
    )
    return list(await session.scalars(query.order_by(WorkerTask.id)))


async def expired_refine_task_ids(
    session: AsyncSession, ids: Collection[int], before: datetime
) -> list[int]:
    query = select(RefineTask.id).where(
        RefineTask.id.in_(ids),
        RefineTask.state.in_(TERMINAL_REFINE_STATES),
        RefineTask.updated_at < before,
    )
    return list(await session.scalars(query.order_by(RefineTask.id)))


async def expired_run_ids(
    session: AsyncSession, ids: Collection[int], before: datetime
) -> list[int]:
    query = select(Run.id).where(
        Run.id.in_(ids), Run.state != RunState.RUNNING, Run.finished_at < before
    )
    return list(await session.scalars(query.order_by(Run.id)))
