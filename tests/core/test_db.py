from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from skeleton_crew.core.db import Base, run_migrations
from skeleton_crew.domain.task_state import RunState
from skeleton_crew.models import Run, WorkerTask


def _diff(connection: Connection) -> list[object]:
    return list(compare_metadata(MigrationContext.configure(connection), Base.metadata))


async def test_migrations_match_models(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        assert await connection.run_sync(_diff) == []
    assert set(Base.metadata.tables) == {
        "settings",
        "rule_revisions",
        "runs",
        "worker_tasks",
        "refine_tasks",
        "audit_events",
    }


async def test_migrations_are_idempotent(engine: AsyncEngine) -> None:
    await run_migrations(engine)
    async with engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == "0006"


async def test_pragmas_are_applied(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        assert await connection.scalar(text("PRAGMA journal_mode")) == "wal"
        assert await connection.scalar(text("PRAGMA foreign_keys")) == 1


async def test_foreign_keys_are_enforced(session: AsyncSession) -> None:
    session.add(WorkerTask(run_id=999, issue_number=1, issue_title="t", state="queued"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_datetimes_roundtrip_as_utc(session: AsyncSession) -> None:
    started = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    run = Run(trigger="manual", dry_run=True, state=RunState.RUNNING, started_at=started)
    session.add(run)
    await session.commit()
    session.expunge_all()
    loaded = await session.get(Run, run.id)
    assert loaded is not None
    assert loaded.started_at == started
    assert loaded.started_at.tzinfo is UTC
    assert loaded.state is RunState.RUNNING


async def test_naive_datetime_is_rejected(session: AsyncSession) -> None:
    naive = datetime(2026, 1, 1)
    session.add(Run(trigger="manual", dry_run=True, state=RunState.RUNNING, started_at=naive))
    with pytest.raises(StatementError):
        await session.flush()


async def test_unknown_enum_value_is_rejected(session: AsyncSession) -> None:
    session.add(Run(trigger="manual", dry_run=True, state="bogus"))
    with pytest.raises(StatementError):
        await session.flush()
