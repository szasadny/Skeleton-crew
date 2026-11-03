import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fake_control import FakeRunControl
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.core.config import BootstrapConfig
from skeleton_crew.core.security import hash_password
from skeleton_crew.domain.settings import AppSettings
from skeleton_crew.scheduler.scheduler import RunScheduler
from skeleton_crew.store.settings_store import load_settings
from skeleton_crew.web.app import create_app

PASSWORD = "correct horse battery staple"  # noqa: S105
TOKEN_SECRET = "oauth-token-value-for-tests"  # noqa: S105
_HASH = hash_password(PASSWORD)
_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def make_config(data_dir: Path, **overrides: Any) -> BootstrapConfig:
    values: dict[str, Any] = {
        "repo": "acme/widgets",
        "github_app_id": 1,
        "github_app_private_key_path": "/nonexistent/key.pem",
        "ui_password_hash": _HASH,
        "session_secret": "s" * 32,
        "claude_oauth_token": TOKEN_SECRET,
        "data_dir": data_dir,
        "insecure_cookies": True,
    }
    values.update(overrides)
    return BootstrapConfig.model_validate(values)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _noop() -> None:
    return None


@dataclass
class Web:
    client: httpx.AsyncClient
    app: FastAPI
    config: BootstrapConfig
    sessionmaker: async_sessionmaker[AsyncSession]
    controller: FakeRunControl
    scheduler: RunScheduler
    clock: FakeClock = field(default_factory=FakeClock)

    async def csrf(self, path: str = "/login") -> str:
        response = await self.client.get(path)
        match = _CSRF_RE.search(response.text)
        assert match is not None, response.text
        return match.group(1)

    async def login(self) -> None:
        token = await self.csrf()
        response = await self.client.post(
            "/login", data={"password": PASSWORD, "csrf_token": token}
        )
        assert response.status_code == 303, response.text

    async def post(self, path: str, data: Mapping[str, Any] | None = None) -> httpx.Response:
        token = await self.csrf("/audit")
        return await self.client.post(path, data={**(data or {}), "csrf_token": token})

    async def settings(self) -> AppSettings:
        async with self.sessionmaker() as session:
            return await load_settings(session)


@asynccontextmanager
async def open_web(
    data_dir: Path,
    sessionmaker: async_sessionmaker[AsyncSession],
    controller: FakeRunControl | None = None,
    **config_overrides: Any,
) -> AsyncIterator[Web]:
    config = make_config(data_dir, **config_overrides)
    control = controller or FakeRunControl()
    scheduler = RunScheduler(_noop)
    app = create_app(
        config=config, sessionmaker=sessionmaker, controller=control, scheduler=scheduler
    )
    clock = FakeClock()
    app.state.web.throttle.clock = clock
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield Web(client, app, config, sessionmaker, control, scheduler, clock)
