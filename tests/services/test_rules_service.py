import pytest
from audit_helpers import audit_events
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.agents.rules import default_rules
from skeleton_crew.core.audit import AuditKind
from skeleton_crew.domain.settings import MAX_RULES_CHARS, InvalidRules
from skeleton_crew.services import rules_service
from skeleton_crew.services.errors import NotFound
from skeleton_crew.store import rules_store


def test_effective_rules_prefer_the_saved_revision() -> None:
    assert rules_service.effective_rules("worker", "Mine.") == "Mine."
    assert rules_service.effective_rules("worker", None) == default_rules("worker")
    assert rules_service.effective_rules("worker", "") == default_rules("worker")


async def test_editor_starts_from_the_default(session: AsyncSession) -> None:
    editor = await rules_service.editor(session, "common")
    assert editor.content == default_rules("common")
    assert editor.is_default is True
    assert editor.history == []


async def test_save_commits_a_revision_and_audits_it(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        await rules_service.save_rules(session, "worker", "Be careful.")
    async with sessionmaker() as session:
        editor = await rules_service.editor(session, "worker")
        events = await audit_events(session, AuditKind.RULES_CHANGED)
    assert (editor.content, editor.is_default) == ("Be careful.", False)
    assert [(e.summary, e.payload) for e in events] == [
        ("rules changed: worker", {"key": "worker", "revision_id": editor.history[0].id})
    ]


async def test_saving_the_same_rules_again_is_skipped(session: AsyncSession) -> None:
    await rules_service.save_rules(session, "reviewer", "r1")
    await rules_service.save_rules(session, "reviewer", "r1")
    assert len((await rules_service.editor(session, "reviewer")).history) == 1
    assert len(await audit_events(session)) == 1


async def test_history_is_capped(session: AsyncSession) -> None:
    for n in range(rules_service.HISTORY_LIMIT + 3):
        await rules_service.save_rules(session, "worker", f"v{n}")
    history = (await rules_service.editor(session, "worker")).history
    assert len(history) == rules_service.HISTORY_LIMIT
    assert history[0].content == f"v{rules_service.HISTORY_LIMIT + 2}"


@pytest.mark.parametrize("content", ["", " \n", "x" * (MAX_RULES_CHARS + 1)])
async def test_invalid_rules_are_neither_saved_nor_audited(
    session: AsyncSession, content: str
) -> None:
    with pytest.raises(InvalidRules):
        await rules_service.save_rules(session, "worker", content)
    assert await rules_store.active_rules(session, "worker") is None
    assert await audit_events(session) == []


async def test_reset_saves_the_default_as_a_revision(session: AsyncSession) -> None:
    await rules_service.save_rules(session, "selector", "Custom")
    await rules_service.reset_rules(session, "selector")
    editor = await rules_service.editor(session, "selector")
    assert (editor.content, editor.is_default, len(editor.history)) == (
        default_rules("selector"),
        False,
        2,
    )


@pytest.mark.parametrize("key", ["admin", "Worker", "", "../worker", "common/../worker"])
async def test_unknown_key_is_not_found_everywhere(session: AsyncSession, key: str) -> None:
    with pytest.raises(NotFound):
        await rules_service.editor(session, key)
    with pytest.raises(NotFound):
        await rules_service.save_rules(session, key, "x")
    with pytest.raises(NotFound):
        await rules_service.save_rules(session, key, "")
    with pytest.raises(NotFound):
        await rules_service.reset_rules(session, key)
    with pytest.raises(NotFound):
        await rules_service.revision(session, key, 1)
    assert await audit_events(session) == []


async def test_revision_is_found_only_under_its_own_key(session: AsyncSession) -> None:
    await rules_service.save_rules(session, "worker", "first")
    first = (await rules_service.editor(session, "worker")).history[0]
    assert (await rules_service.revision(session, "worker", first.id)).content == "first"
    with pytest.raises(NotFound):
        await rules_service.revision(session, "common", first.id)
    with pytest.raises(NotFound):
        await rules_service.revision(session, "worker", first.id + 1000)
