from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from skeleton_crew.core.db import alembic_config, make_engine

OLD_AT = "2026-01-01 00:00:00.000000"


def upgrade(connection: Connection, revision: str) -> None:
    command.upgrade(alembic_config(connection), revision)


def downgrade(connection: Connection, revision: str) -> None:
    command.downgrade(alembic_config(connection), revision)


def columns(table: str) -> Callable[[Connection], set[str]]:
    def read(connection: Connection) -> set[str]:
        query = text("SELECT name FROM pragma_table_info(:table)")
        return set(connection.execute(query, {"table": table}).scalars())

    return read


async def seed_old_run(connection: AsyncConnection) -> None:
    await connection.execute(
        text(
            "INSERT INTO runs (id, trigger, dry_run, state, started_at)"
            " VALUES (1, 'manual', 0, 'completed', :at)"
        ),
        {"at": OLD_AT},
    )


@asynccontextmanager
async def old_engine(directory: Path, revision: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(directory / f"old-{revision}.db")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(upgrade, revision)
        yield engine
    finally:
        await engine.dispose()
