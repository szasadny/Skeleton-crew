from collections.abc import AsyncIterator
from importlib.resources import files
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from skeleton_crew.core.db import make_engine, make_sessionmaker
from skeleton_crew.store import runs_store

_AT = "2026-01-01 00:00:00.000000"


def _config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(files("skeleton_crew") / "migrations"))
    config.attributes["connection"] = connection
    return config


def _upgrade(connection: Connection, revision: str) -> None:
    command.upgrade(_config(connection), revision)


def _downgrade(connection: Connection, revision: str) -> None:
    command.downgrade(_config(connection), revision)


def _old_reason(connection: Connection) -> object:
    return connection.execute(text("SELECT escalation_reason FROM worker_tasks")).scalar_one()


@pytest.fixture
async def old_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(tmp_path / "old.db")
    async with engine.begin() as connection:
        await connection.run_sync(_upgrade, "0003")
        await connection.execute(
            text(
                "INSERT INTO runs (id, trigger, dry_run, state, started_at)"
                " VALUES (1, 'manual', 0, 'completed', :at)"
            ),
            {"at": _AT},
        )
        await connection.execute(
            text(
                "INSERT INTO worker_tasks (id, run_id, issue_number, issue_title, state, kind,"
                " ci_rounds, review_rounds, policy_rounds, cost_usd, escalation_reason,"
                " created_at, updated_at)"
                " VALUES (1, 1, 7, 'Fix it', 'escalated', 'issue', 0, 0, 0, 0.0,"
                " 'CI still fails', :at, :at)"
            ),
            {"at": _AT},
        )
    yield engine
    await engine.dispose()


async def test_escalation_reason_moves_into_note_and_back(old_engine: AsyncEngine) -> None:
    async with old_engine.begin() as connection:
        await connection.run_sync(_upgrade, "head")
    async with make_sessionmaker(old_engine)() as session:
        task = await runs_store.get_worker_task(session, 1)
    assert task is not None
    assert task.note == "CI still fails"
    async with old_engine.begin() as connection:
        await connection.run_sync(_downgrade, "0003")
        assert await connection.run_sync(_old_reason) == "CI still fails"
