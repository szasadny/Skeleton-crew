import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web_helpers import make_app, make_config

from skeleton_crew.domain.settings import AppSettings, Schedule, Weekday
from skeleton_crew.scheduler.scheduler import RunScheduler
from skeleton_crew.store.settings_store import save_settings


async def test_lifespan_starts_scheduler_from_stored_settings_and_stops_it(
    tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as session:
        await save_settings(
            session, AppSettings(schedule=Schedule(weekdays=frozenset({Weekday.WED})))
        )
        await session.commit()
    app = make_app(make_config(tmp_path), sessionmaker)
    scheduler: RunScheduler = app.state.web.scheduler
    async with app.router.lifespan_context(app):
        assert scheduler.running
        next_run = scheduler.next_run_time()
        assert next_run is not None
        assert next_run.weekday() == 2
    await asyncio.sleep(0)
    assert not scheduler.running


def test_api_docs_are_disabled(tmp_path: Path) -> None:
    app = make_app(make_config(tmp_path), async_sessionmaker())
    assert app.openapi_url is None
    assert app.docs_url is None
