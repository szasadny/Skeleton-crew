from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.domain.task_state import RefineState, TaskEvent, TaskState, transition
from skeleton_crew.store import runs_store


class WorkerTaskRecord:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], task_id: int) -> None:
        self._sessionmaker = sessionmaker
        self.task_id = task_id
        self.state = TaskState.QUEUED
        self.cost_usd = 0.0

    async def update(self, **fields: object) -> None:
        async with self._sessionmaker() as session:
            await runs_store.update_worker_task(session, self.task_id, **fields)
            await session.commit()

    async def fire(self, event: TaskEvent, **fields: object) -> None:
        state = transition(self.state, event)
        await self.update(state=state, **fields)
        self.state = state

    async def add_cost(self, cost_usd: float, **fields: object) -> None:
        self.cost_usd += cost_usd
        await self.update(cost_usd=self.cost_usd, **fields)


class RefineTaskRecord:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], task_id: int) -> None:
        self._sessionmaker = sessionmaker
        self.task_id = task_id

    async def update(self, **fields: object) -> None:
        async with self._sessionmaker() as session:
            await runs_store.update_refine_task(session, self.task_id, **fields)
            await session.commit()

    async def set_state(self, state: RefineState, **fields: object) -> None:
        await self.update(state=state, **fields)
