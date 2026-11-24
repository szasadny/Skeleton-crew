from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.domain.task_state import (
    RefineState,
    TaskEvent,
    TaskKind,
    TaskState,
    cap_note,
    merge_blockers,
    transition,
)
from skeleton_crew.github.gateway import IssueRef
from skeleton_crew.models.worker_task import ISSUE_TITLE_LENGTH
from skeleton_crew.store import runs_store

MAX_STORED_BLOCKERS = 30


class _TaskRecord(ABC):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], task_id: int) -> None:
        self._sessionmaker = sessionmaker
        self.task_id = task_id
        self.cost_usd = 0.0

    @abstractmethod
    async def _write(self, session: AsyncSession, **fields: object) -> None: ...

    async def update(self, **fields: object) -> None:
        async with self._sessionmaker() as session:
            await self._write(session, **fields)
            await session.commit()

    async def add_cost(self, cost_usd: float, **fields: object) -> None:
        self.cost_usd += cost_usd
        await self.update(cost_usd=self.cost_usd, **fields)


class WorkerTaskRecord(_TaskRecord):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], task_id: int) -> None:
        super().__init__(sessionmaker, task_id)
        self.state = TaskState.QUEUED
        self.blockers: list[str] = []

    @classmethod
    async def create(
        cls,
        sessionmaker: async_sessionmaker[AsyncSession],
        run_id: int,
        issue: IssueRef,
        kind: TaskKind,
    ) -> Self:
        title = issue.title[:ISSUE_TITLE_LENGTH]
        async with sessionmaker() as session:
            task = await runs_store.add_worker_task(session, run_id, issue.number, title, kind)
            await session.commit()
        return cls(sessionmaker, task.id)

    async def _write(self, session: AsyncSession, **fields: object) -> None:
        await runs_store.update_worker_task(session, self.task_id, **fields)

    async def fire(self, event: TaskEvent, **fields: object) -> None:
        state = transition(self.state, event)
        await self.update(state=state, **fields)
        self.state = state

    async def end(self, event: TaskEvent, note: str) -> None:
        await self.fire(event, note=cap_note(note))

    async def add_blockers(self, blockers: Sequence[str]) -> None:
        merged = merge_blockers(self.blockers, blockers, MAX_STORED_BLOCKERS)
        if len(merged) == len(self.blockers):
            return
        self.blockers = merged
        await self.update(blockers=list(self.blockers))


class RefineTaskRecord(_TaskRecord):
    @classmethod
    async def create(
        cls, sessionmaker: async_sessionmaker[AsyncSession], run_id: int, issue_number: int
    ) -> Self:
        async with sessionmaker() as session:
            task = await runs_store.add_refine_task(session, run_id, issue_number)
            await session.commit()
        return cls(sessionmaker, task.id)

    async def _write(self, session: AsyncSession, **fields: object) -> None:
        await runs_store.update_refine_task(session, self.task_id, **fields)

    async def end(self, state: RefineState, error: str) -> None:
        await self.update(state=state, error=cap_note(error))
