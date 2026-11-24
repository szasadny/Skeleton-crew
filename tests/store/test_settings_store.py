from audit_helpers import audit_events
from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.domain.settings import AppSettings, Limits
from skeleton_crew.store.settings_store import load_settings, save_settings


async def test_defaults_when_no_row(session: AsyncSession) -> None:
    assert await load_settings(session) == AppSettings()


async def test_save_and_load_roundtrip(session: AsyncSession) -> None:
    settings = AppSettings(dry_run=False, limits=Limits(issues_per_day=7))
    await save_settings(session, settings)
    await session.commit()
    assert await load_settings(session) == settings


async def test_save_overwrites_the_single_row(session: AsyncSession) -> None:
    await save_settings(session, AppSettings(dry_run=False))
    await save_settings(session, AppSettings(kill_switch=True))
    assert await load_settings(session) == AppSettings(kill_switch=True)


async def test_the_store_leaves_auditing_to_its_callers(session: AsyncSession) -> None:
    await save_settings(session, AppSettings(kill_switch=True))
    assert await audit_events(session) == []
