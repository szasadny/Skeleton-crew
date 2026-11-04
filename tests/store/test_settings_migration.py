import json
from collections.abc import AsyncIterator
from importlib.resources import files
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from skeleton_crew.core.db import make_engine, make_sessionmaker
from skeleton_crew.domain.settings import Role
from skeleton_crew.store.settings_store import load_settings

_OLD = {
    "agents": {
        role.value: {"backend": "claude_code", "model": "sonnet", "effort": "high"} for role in Role
    },
    "claude_auth_mode": "api_key",
    "dry_run": False,
}


def _upgrade(connection: Connection, revision: str) -> None:
    command.upgrade(_config(connection), revision)


def _downgrade(connection: Connection, revision: str) -> None:
    command.downgrade(_config(connection), revision)


def _config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(files("skeleton_crew") / "migrations"))
    config.attributes["connection"] = connection
    return config


def _stored(connection: Connection) -> object:
    return json.loads(connection.execute(text("SELECT data FROM settings")).scalar_one())


@pytest.fixture
async def old_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(tmp_path / "old.db")
    async with engine.begin() as connection:
        await connection.run_sync(_upgrade, "0002")
    yield engine
    await engine.dispose()


async def _seed(engine: AsyncEngine, data: object) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO settings (id, data, updated_at) VALUES (1, :data, :at)"),
            {"data": json.dumps(data), "at": "2026-01-01 00:00:00.000000"},
        )
        await connection.run_sync(_upgrade, "head")


async def test_auth_mode_moves_into_backend_options_and_back(old_engine: AsyncEngine) -> None:
    await _seed(old_engine, _OLD)
    async with make_sessionmaker(old_engine)() as session:
        settings = await load_settings(session)
    assert settings.backend_options == {"claude_code": {"auth_mode": "api_key"}}
    assert settings.agents[Role.WORKER].model == "sonnet"
    assert settings.dry_run is False
    async with old_engine.begin() as connection:
        await connection.run_sync(_downgrade, "0002")
        assert await connection.run_sync(_stored) == _OLD


async def test_upgrade_without_auth_mode_leaves_options_empty(old_engine: AsyncEngine) -> None:
    await _seed(old_engine, {})
    async with make_sessionmaker(old_engine)() as session:
        assert (await load_settings(session)).backend_options == {}
