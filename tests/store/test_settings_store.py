from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.core.audit import AuditKind
from skeleton_crew.domain.settings import AppSettings, Limits, RepoPolicy
from skeleton_crew.store import audit_store
from skeleton_crew.store.settings_store import load_settings, save_settings


async def test_defaults_when_no_row(session: AsyncSession) -> None:
    assert await load_settings(session) == AppSettings()


async def test_save_and_load_roundtrip(session: AsyncSession) -> None:
    settings = AppSettings(dry_run=False, limits=Limits(issues_per_day=7))
    await save_settings(session, settings)
    await session.commit()
    assert await load_settings(session) == settings


async def test_audit_lists_changed_top_level_keys_without_values(session: AsyncSession) -> None:
    await save_settings(session, AppSettings(repo=RepoPolicy(setup_command="make secret-setup")))
    await save_settings(
        session,
        AppSettings(kill_switch=True, repo=RepoPolicy(setup_command="make secret-setup")),
    )
    events = await audit_store.list_events(session, limit=10, offset=0)
    assert [e.kind for e in events] == [AuditKind.SETTINGS_CHANGED] * 2
    assert [e.payload for e in events] == [{"keys": ["kill_switch"]}, {"keys": ["repo"]}]
    assert all("secret-setup" not in e.summary for e in events)


async def test_saving_unchanged_settings_is_not_audited(session: AsyncSession) -> None:
    await save_settings(session, AppSettings())
    await save_settings(session, AppSettings())
    assert await audit_store.list_events(session, limit=10, offset=0) == []
    assert await load_settings(session) == AppSettings()
