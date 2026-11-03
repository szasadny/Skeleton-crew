import asyncio
from pathlib import Path

from fake_control import FakeRunControl
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web_helpers import make_config

from skeleton_crew.domain.settings import AppSettings, Schedule, Weekday
from skeleton_crew.scheduler.scheduler import RunScheduler
from skeleton_crew.store.settings_store import save_settings
from skeleton_crew.web.app import create_app


async def _noop() -> None:
    return None


async def test_lifespan_starts_scheduler_from_stored_settings_and_stops_it(
    tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as session:
        await save_settings(
            session, AppSettings(schedule=Schedule(weekdays=frozenset({Weekday.WED})))
        )
        await session.commit()
    scheduler = RunScheduler(_noop)
    app = create_app(
        config=make_config(tmp_path),
        sessionmaker=sessionmaker,
        controller=FakeRunControl(),
        scheduler=scheduler,
    )
    async with app.router.lifespan_context(app):
        assert scheduler.running
        next_run = scheduler.next_run_time()
        assert next_run is not None
        assert next_run.weekday() == 2
    await asyncio.sleep(0)
    assert not scheduler.running


def test_api_docs_are_disabled(tmp_path: Path) -> None:
    app = create_app(
        config=make_config(tmp_path),
        sessionmaker=async_sessionmaker(),
        controller=FakeRunControl(),
        scheduler=RunScheduler(_noop),
    )
    assert app.openapi_url is None
    assert app.docs_url is None
