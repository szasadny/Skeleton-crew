import pytest
from audit_helpers import audit_events
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.core.audit import AuditKind
from skeleton_crew.domain.settings import AppSettings, Limits, RepoPolicy, with_updates
from skeleton_crew.services.errors import GENERAL
from skeleton_crew.services.settings_service import (
    InvalidSettings,
    engage_kill_switch,
    set_flags,
    update_settings,
)
from skeleton_crew.store import settings_store
from skeleton_crew.store.settings_store import load_settings, save_settings


async def _stored(sessionmaker: async_sessionmaker[AsyncSession]) -> AppSettings:
    async with sessionmaker() as session:
        return await load_settings(session)


def test_with_updates_sets_dotted_fields_and_keeps_the_rest() -> None:
    current = AppSettings(dry_run=False)
    updated = with_updates(current, {"limits.issues_per_day": "7", "repo.require_ci": False})
    assert updated.limits.issues_per_day == 7
    assert updated.repo.require_ci is False
    assert updated.dry_run is False
    assert current.limits == Limits()


def test_with_updates_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        with_updates(AppSettings(), {"limits.bogus": 1})


async def test_update_saves_commits_and_audits_changed_keys_only(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        saved = await update_settings(
            session, {"limits.issues_per_day": "7", "repo.setup_command": "make secret-setup"}
        )
    assert await _stored(sessionmaker) == saved
    async with sessionmaker() as session:
        events = await audit_events(session, AuditKind.SETTINGS_CHANGED)
    assert [e.payload for e in events] == [{"keys": ["limits", "repo"]}]
    assert events[0].summary == "settings changed: limits, repo"
    assert "secret-setup" not in repr([(e.summary, e.payload) for e in events])


async def test_unchanged_update_is_saved_but_not_audited(session: AsyncSession) -> None:
    await update_settings(session, {"limits.issues_per_day": str(Limits().issues_per_day)})
    assert await audit_events(session) == []


@pytest.mark.parametrize(
    ("updates", "field"),
    [
        ({"limits.issues_per_day": "999"}, "limits.issues_per_day"),
        ({"limits.issues_per_day": "many"}, "limits.issues_per_day"),
        ({"schedule.timezone": "Mars/Olympus"}, "schedule.timezone"),
    ],
)
async def test_invalid_value_is_reported_on_its_field_and_not_saved(
    session: AsyncSession, updates: dict[str, object], field: str
) -> None:
    with pytest.raises(InvalidSettings) as caught:
        await update_settings(session, updates)
    assert list(caught.value.errors) == [field]
    assert await load_settings(session) == AppSettings()
    assert await audit_events(session) == []


async def test_model_level_error_is_general_and_drops_the_pydantic_prefix(
    session: AsyncSession,
) -> None:
    with pytest.raises(InvalidSettings) as caught:
        await update_settings(session, {"labels.hitl": "Skeleton", "labels.ready": "skeleton"})
    assert caught.value.errors == {GENERAL: ["label names must be distinct"]}


async def test_check_problems_map_to_known_fields_or_general(session: AsyncSession) -> None:
    def check(_settings: AppSettings) -> dict[str, list[str]]:
        return {"repo.base_branch": ["not here"], "agents.x.y": ["elsewhere"]}

    with pytest.raises(InvalidSettings) as caught:
        await update_settings(session, {"repo.base_branch": "dev"}, check)
    assert caught.value.errors == {"repo.base_branch": ["not here"], GENERAL: ["elsewhere"]}
    assert await load_settings(session) == AppSettings()


async def test_check_sees_the_new_settings_and_on_saved_runs_only_after_a_save(
    session: AsyncSession,
) -> None:
    checked: list[str] = []
    saved: list[AppSettings] = []

    def check(settings: AppSettings) -> dict[str, list[str]]:
        checked.append(settings.repo.base_branch)
        return {"repo.base_branch": ["no"]} if settings.repo.base_branch == "bad" else {}

    with pytest.raises(InvalidSettings):
        await update_settings(session, {"repo.base_branch": "bad"}, check, saved.append)
    assert saved == []
    new = await update_settings(session, {"repo.base_branch": "dev"}, check, saved.append)
    assert checked == ["bad", "dev"]
    assert saved == [new]


async def test_dry_run_flag_is_saved_without_a_kill_switch_event(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        await set_flags(session, dry_run=False)
    assert (await _stored(sessionmaker)).dry_run is False
    async with sessionmaker() as session:
        events = await audit_events(session)
    assert [(e.kind, e.payload) for e in events] == [
        (AuditKind.SETTINGS_CHANGED, {"keys": ["dry_run"]})
    ]


async def test_kill_switch_records_the_change_then_the_kill_switch_event(
    session: AsyncSession,
) -> None:
    await set_flags(session, kill_switch=True)
    await set_flags(session, kill_switch=True)
    await set_flags(session, kill_switch=False)
    events = [(e.kind, e.summary) for e in await audit_events(session)]
    assert events == [
        (AuditKind.SETTINGS_CHANGED, "settings changed: kill_switch"),
        (AuditKind.KILL_SWITCH, "kill switch engaged from the UI"),
        (AuditKind.KILL_SWITCH, "kill switch engaged from the UI"),
        (AuditKind.SETTINGS_CHANGED, "settings changed: kill_switch"),
        (AuditKind.KILL_SWITCH, "kill switch released"),
    ]
    assert (await load_settings(session)).kill_switch is False


async def test_flags_leave_other_settings_alone(session: AsyncSession) -> None:
    custom = AppSettings(repo=RepoPolicy(base_branch="dev"))
    await save_settings(session, custom)
    await set_flags(session, dry_run=False)
    assert await load_settings(session) == custom.model_copy(update={"dry_run": False})


async def test_no_flags_change_nothing(session: AsyncSession) -> None:
    assert await set_flags(session) == AppSettings()
    assert await audit_events(session) == []


async def test_kill_switch_is_saved_and_audited_before_the_run_stops(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    seen: list[tuple[bool, list[str]]] = []

    async def stop() -> None:
        async with sessionmaker() as other:
            events = [e.summary for e in await audit_events(other)]
            seen.append(((await load_settings(other)).kill_switch, events))

    async with sessionmaker() as session:
        await engage_kill_switch(session, stop)
    assert seen == [(True, ["settings changed: kill_switch", "kill switch engaged from the UI"])]


async def test_a_failed_kill_switch_save_does_not_stop_the_run(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    stops: list[bool] = []

    async def stop() -> None:
        stops.append(True)

    async def broken(_session: AsyncSession, _settings: AppSettings) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(settings_store, "save_settings", broken)
    with pytest.raises(RuntimeError):
        await engage_kill_switch(session, stop)
    assert stops == []
