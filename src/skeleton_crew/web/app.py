from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from skeleton_crew.core.config import BootstrapConfig
from skeleton_crew.scheduler.scheduler import RunScheduler
from skeleton_crew.store.settings_store import load_settings
from skeleton_crew.web.ports import RunControl
from skeleton_crew.web.routes import agents, audit, auth, dashboard, limits, repo, rules, runs
from skeleton_crew.web.routes import schedule as schedule_page
from skeleton_crew.web.security import (
    LOGIN_PATH,
    SESSION_MAX_AGE_S,
    LoginRequired,
    LoginThrottle,
    SecurityHeadersMiddleware,
    verify_request,
)
from skeleton_crew.web.state import WebState, get_state

_STATIC_DIR = Path(__file__).parent / "static"
_ROUTERS = (auth, dashboard, schedule_page, limits, agents, repo, rules, runs, audit)


async def _guard(request: Request) -> None:
    await verify_request(request, get_state(request).config.session_secret.get_secret_value())


async def _login_required(request: Request, _exc: Exception) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=401, headers={"HX-Redirect": LOGIN_PATH})
    return RedirectResponse(LOGIN_PATH, status_code=303)


def create_app(
    *,
    config: BootstrapConfig,
    sessionmaker: async_sessionmaker[AsyncSession],
    controller: RunControl,
    scheduler: RunScheduler,
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
        dependencies=[Depends(_guard)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.web = WebState(config, sessionmaker, controller, scheduler, LoginThrottle())
    app.add_exception_handler(LoginRequired, _login_required)
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
