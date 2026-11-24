from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.core.config import BootstrapConfig
from skeleton_crew.scheduler.scheduler import RunScheduler
from skeleton_crew.services.login_service import LoginGate
from skeleton_crew.web.ports import RunControl, RunnerControl


@dataclass(frozen=True, kw_only=True)
class WebState:
    config: BootstrapConfig
    sessionmaker: async_sessionmaker[AsyncSession]
    controller: RunControl
    scheduler: RunScheduler
    runners: RunnerControl
    logins: LoginGate


def get_state(request: Request) -> WebState:
    state: WebState = request.app.state.web
    return state


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with get_state(request).sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
