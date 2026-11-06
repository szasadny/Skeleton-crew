from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.core.config import BootstrapConfig
from skeleton_crew.scheduler.scheduler import RunScheduler
from skeleton_crew.web.ports import RunControl, RunnerControl
from skeleton_crew.web.security import LoginThrottle


@dataclass(frozen=True)
class WebState:
    config: BootstrapConfig
    sessionmaker: async_sessionmaker[AsyncSession]
    controller: RunControl
    scheduler: RunScheduler
    runners: RunnerControl
    throttle: LoginThrottle


def get_state(request: Request) -> WebState:
    state: WebState = request.app.state.web
    return state
