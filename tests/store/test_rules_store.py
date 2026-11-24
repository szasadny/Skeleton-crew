import pytest
from audit_helpers import audit_events
from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.domain.settings import MAX_RULES_CHARS, InvalidRules
from skeleton_crew.store.rules_store import active_rules, get_revision, rule_history, save_rules


async def test_newest_revision_is_active(session: AsyncSession) -> None:
    assert await active_rules(session, "worker") is None
    await save_rules(session, "worker", "v1")
    await save_rules(session, "worker", "v2")
    await save_rules(session, "common", "c1")
    assert await active_rules(session, "worker") == "v2"
    assert [r.content for r in await rule_history(session, "worker", limit=10)] == ["v2", "v1"]
    assert [r.content for r in await rule_history(session, "worker", limit=1)] == ["v2"]


async def test_unchanged_save_is_skipped_and_nothing_is_audited(session: AsyncSession) -> None:
    first = await save_rules(session, "reviewer", "r1")
    assert first is not None
    assert (first.role, first.content) == ("reviewer", "r1")
    assert await save_rules(session, "reviewer", "r1") is None
    assert len(await rule_history(session, "reviewer", limit=10)) == 1
    assert await audit_events(session) == []


@pytest.mark.parametrize("key", ["admin", "Worker", "", "../worker"])
async def test_unknown_key_is_rejected(session: AsyncSession, key: str) -> None:
    with pytest.raises(ValueError, match="unknown rules key"):
        await save_rules(session, key, "x")
    with pytest.raises(ValueError, match="unknown rules key"):
        await rule_history(session, key, limit=1)
    with pytest.raises(ValueError, match="unknown rules key"):
        await get_revision(session, key, 1)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "cannot be empty"),
        (" \n\t\n", "cannot be empty"),
        ("\u3000", "cannot be empty"),
        ("x" * (MAX_RULES_CHARS + 1), f"limited to {MAX_RULES_CHARS} characters"),
    ],
)
async def test_invalid_rules_are_rejected_and_not_stored(
    session: AsyncSession, content: str, message: str
) -> None:
    with pytest.raises(InvalidRules, match=message):
        await save_rules(session, "worker", content)
    assert await active_rules(session, "worker") is None


async def test_rules_at_the_limit_are_saved(session: AsyncSession) -> None:
    await save_rules(session, "worker", "x" * MAX_RULES_CHARS)
    assert await active_rules(session, "worker") == "x" * MAX_RULES_CHARS


async def test_get_revision_finds_old_revisions_of_the_same_key_only(
    session: AsyncSession,
) -> None:
    await save_rules(session, "worker", "first")
    first = (await rule_history(session, "worker", limit=1))[0]
    for n in range(30):
        await save_rules(session, "worker", f"v{n}")
    found = await get_revision(session, "worker", first.id)
    assert found is not None
    assert found.content == "first"
    assert await get_revision(session, "common", first.id) is None
    assert await get_revision(session, "worker", 99_999) is None
