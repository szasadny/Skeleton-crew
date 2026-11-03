from importlib.resources import files
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def _set_sqlite_pragmas(dbapi_connection: Any, _record: object) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_engine(db_path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _alembic_config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(files("skeleton_crew") / "migrations"))
    config.attributes["connection"] = connection
    return config


def _upgrade_head(connection: Connection) -> None:
    command.upgrade(_alembic_config(connection), "head")


async def run_migrations(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_upgrade_head)
