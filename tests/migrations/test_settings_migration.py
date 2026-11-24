import json
from pathlib import Path

from migration_helpers import OLD_AT, downgrade, old_engine, upgrade
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from skeleton_crew.core.db import make_sessionmaker
from skeleton_crew.domain.settings import Role
from skeleton_crew.store.settings_store import load_settings

_REVISION = "0002"
_OLD = {
    "agents": {
        role.value: {"backend": "claude_code", "model": "sonnet", "effort": "high"} for role in Role
    },
    "claude_auth_mode": "api_key",
    "dry_run": False,
}


def _stored(connection: Connection) -> object:
    return json.loads(connection.execute(text("SELECT data FROM settings")).scalar_one())


async def _seed(engine: AsyncEngine, data: object) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO settings (id, data, updated_at) VALUES (1, :data, :at)"),
            {"data": json.dumps(data), "at": OLD_AT},
        )
        await connection.run_sync(upgrade, "head")


async def test_auth_mode_moves_into_backend_options_and_back(tmp_path: Path) -> None:
    async with old_engine(tmp_path, _REVISION) as engine:
        await _seed(engine, _OLD)
        async with make_sessionmaker(engine)() as session:
            settings = await load_settings(session)
        assert settings.backend_options == {"claude_code": {"auth_mode": "api_key"}}
        assert settings.agents[Role.WORKER].model == "sonnet"
        assert settings.dry_run is False
        async with engine.begin() as connection:
            await connection.run_sync(downgrade, _REVISION)
            assert await connection.run_sync(_stored) == _OLD


async def test_upgrade_without_auth_mode_leaves_options_empty(tmp_path: Path) -> None:
    async with old_engine(tmp_path, _REVISION) as engine:
        await _seed(engine, {})
        async with make_sessionmaker(engine)() as session:
            assert (await load_settings(session)).backend_options == {}
