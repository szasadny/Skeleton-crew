from collections.abc import AsyncIterator
from importlib.resources import files
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from skeleton_crew.core.db import make_engine, make_sessionmaker
from skeleton_crew.domain.task_state import TaskKind
from skeleton_crew.store import runs_store

_AT = "2026-01-01 00:00:00.000000"
_KINDS = {1: "issue", 2: "pr_update", 3: "pr_fix"}


def _config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(files("skeleton_crew") / "migrations"))
    config.attributes["connection"] = connection
    return config


def _upgrade(connection: Connection, revision: str) -> None:
    command.upgrade(_config(connection), revision)


def _downgrade(connection: Connection, revision: str) -> None:
    command.downgrade(_config(connection), revision)


def _stored_kinds(connection: Connection) -> dict[int, str]:
    rows = connection.execute(text("SELECT id, kind FROM worker_tasks ORDER BY id")).all()
    return {row[0]: row[1] for row in rows}


@pytest.fixture
async def old_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(tmp_path / "old.db")
    async with engine.begin() as connection:
        await connection.run_sync(_upgrade, "0006")
        await connection.execute(
            text(
                "INSERT INTO runs (id, trigger, dry_run, state, started_at)"
                " VALUES (1, 'manual', 0, 'completed', :at)"
            ),
            {"at": _AT},
        )
        for task_id, kind in _KINDS.items():
            await connection.execute(
                text(
                    "INSERT INTO worker_tasks (id, run_id, issue_number, issue_title, state, kind,"
                    " check_rounds, ci_rounds, review_rounds, policy_rounds, cost_usd, note,"
                    " blockers, created_at, updated_at)"
                    " VALUES (:id, 1, :id, 'Fix it', 'done', :kind, 0, 0, 0, 0, 0.0, NULL, NULL,"
                    " :at, :at)"
                ),
                {"id": task_id, "kind": kind, "at": _AT},
            )
    yield engine
    await engine.dispose()


async def test_pr_update_rows_become_pr_fix_and_stay_so_on_downgrade(
    old_engine: AsyncEngine,
) -> None:
    async with old_engine.begin() as connection:
        await connection.run_sync(_upgrade, "head")
    sessionmaker = make_sessionmaker(old_engine)
    async with sessionmaker() as session:
        tasks = [await runs_store.get_worker_task(session, task_id) for task_id in _KINDS]
    assert [task.kind if task else None for task in tasks] == [
        TaskKind.ISSUE,
        TaskKind.PR_FIX,
        TaskKind.PR_FIX,
    ]
    async with old_engine.begin() as connection:
        await connection.run_sync(_downgrade, "0006")
        assert await connection.run_sync(_stored_kinds) == {1: "issue", 2: "pr_fix", 3: "pr_fix"}
