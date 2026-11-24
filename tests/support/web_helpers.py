import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from audit_helpers import audit_events
from fake_control import FakeRunControl, FakeRunnerControl
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from triggers import noop_trigger

from skeleton_crew.core.audit import AuditKind
from skeleton_crew.core.config import BootstrapConfig
from skeleton_crew.core.security import hash_password
from skeleton_crew.domain.settings import AppSettings
from skeleton_crew.models.audit_event import AuditEvent
from skeleton_crew.scheduler.scheduler import RunScheduler
from skeleton_crew.store.settings_store import load_settings
from skeleton_crew.web.app import create_app
from skeleton_crew.web.security import CSRF_FIELD, LOGIN_PATH

PASSWORD = "correct horse battery staple"  # noqa: S105
TOKEN_SECRET = "oauth-token-value-for-tests"  # noqa: S105
_HASH = hash_password(PASSWORD)
_CSRF_RE = re.compile(rf'name="{CSRF_FIELD}" value="([^"]+)"')


def make_config(data_dir: Path, **overrides: Any) -> BootstrapConfig:
    values: dict[str, Any] = {
        "repo": "acme/widgets",
        "github_app_id": 1,
        "github_app_private_key_path": "/nonexistent/key.pem",
        "ui_password_hash": _HASH,
        "session_secret": "s" * 32,
        "agent_credentials": {"claude_oauth_token": TOKEN_SECRET},
        "data_dir": data_dir,
        "insecure_cookies": True,
    }
    values.update(overrides)
    return BootstrapConfig.model_validate(values)


def make_app(
    config: BootstrapConfig,
    sessionmaker: async_sessionmaker[AsyncSession],
    controller: FakeRunControl | None = None,
    runners: FakeRunnerControl | None = None,
) -> FastAPI:
    return create_app(
        config=config,
        sessionmaker=sessionmaker,
        controller=controller or FakeRunControl(),
        scheduler=RunScheduler(noop_trigger),
        runners=runners or FakeRunnerControl(),
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Web:
    client: httpx.AsyncClient
    app: FastAPI
    config: BootstrapConfig
    sessionmaker: async_sessionmaker[AsyncSession]
    controller: FakeRunControl
    scheduler: RunScheduler
    clock: FakeClock = field(default_factory=FakeClock)

    async def csrf(self, path: str = LOGIN_PATH) -> str:
        response = await self.client.get(path)
        match = _CSRF_RE.search(response.text)
        assert match is not None, response.text
        return match.group(1)

    async def login(self) -> None:
        token = await self.csrf()
        response = await self.client.post(
            LOGIN_PATH, data={"password": PASSWORD, CSRF_FIELD: token}
        )
        assert response.status_code == 303, response.text

    async def post(self, path: str, data: Mapping[str, Any] | None = None) -> httpx.Response:
        token = await self.csrf("/logs")
        return await self.client.post(path, data={**(data or {}), CSRF_FIELD: token})

    async def settings(self) -> AppSettings:
        async with self.sessionmaker() as session:
            return await load_settings(session)

    async def audit(self, kind: AuditKind) -> list[AuditEvent]:
        async with self.sessionmaker() as session:
            return await audit_events(session, kind)


type WebFactory = Callable[..., AbstractAsyncContextManager[Web]]


@asynccontextmanager
async def open_web(
    data_dir: Path,
    sessionmaker: async_sessionmaker[AsyncSession],
    controller: FakeRunControl | None = None,
    runners: FakeRunnerControl | None = None,
    *,
    login: bool = False,
    **config_overrides: Any,
) -> AsyncIterator[Web]:
    config = make_config(data_dir, **config_overrides)
    control = controller or FakeRunControl()
    app = make_app(config, sessionmaker, control, runners)
    clock = FakeClock()
    app.state.web.logins.throttle.clock = clock
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        web = Web(client, app, config, sessionmaker, control, app.state.web.scheduler, clock)
        if login:
            await web.login()
        yield web
