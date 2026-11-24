from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from skeleton_crew.core.config import BootstrapConfig
from skeleton_crew.scheduler.scheduler import RunScheduler
from skeleton_crew.services.login_service import LoginGate
from skeleton_crew.store.settings_store import load_settings
from skeleton_crew.web.errors import register_error_handlers
from skeleton_crew.web.ports import RunControl, RunnerControl
from skeleton_crew.web.routes import (
    agents,
    auth,
    blockers,
    ci,
    dashboard,
    limits,
    logs,
    repo,
    rules,
    runs,
)
from skeleton_crew.web.routes import schedule as schedule_page
from skeleton_crew.web.security import (
    SESSION_MAX_AGE_S,
    SecurityHeadersMiddleware,
    require_csrf,
    require_login,
)
from skeleton_crew.web.state import WebState

_STATIC_DIR = Path(__file__).parent / "static"
_ROUTERS = (auth, dashboard, schedule_page, limits, agents, repo, ci, rules, runs, blockers, logs)


def create_app(
    *,
    config: BootstrapConfig,
    sessionmaker: async_sessionmaker[AsyncSession],
    controller: RunControl,
    scheduler: RunScheduler,
    runners: RunnerControl,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with sessionmaker() as session:
            settings = await load_settings(session)
        scheduler.start(settings)
        try:
            yield
        finally:
            scheduler.shutdown()

    app = FastAPI(
        lifespan=lifespan,
        dependencies=[Depends(require_login), Depends(require_csrf)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.web = WebState(
        config=config,
        sessionmaker=sessionmaker,
        controller=controller,
        scheduler=scheduler,
        runners=runners,
        logins=LoginGate(config.ui_password_hash),
    )
    register_error_handlers(app)
    for module in _ROUTERS:
        app.include_router(module.router)
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret.get_secret_value(),
        max_age=SESSION_MAX_AGE_S,
        same_site="strict",
        https_only=not config.insecure_cookies,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    return app
