import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.core.audit import AuditKind
from skeleton_crew.domain.settings import RULE_KEYS
from skeleton_crew.store import audit_store
from skeleton_crew.store.rules_store import active_rules, rule_history, save_rules


def test_rule_keys_cover_common_and_every_role() -> None:
    assert RULE_KEYS == ("common", "selector", "refiner", "reviewer", "worker")


async def test_newest_revision_is_active(session: AsyncSession) -> None:
    assert await active_rules(session, "worker") is None
    await save_rules(session, "worker", "v1")
    await save_rules(session, "worker", "v2")
    await save_rules(session, "common", "c1")
    assert await active_rules(session, "worker") == "v2"
    assert [r.content for r in await rule_history(session, "worker", limit=10)] == ["v2", "v1"]
    assert [r.content for r in await rule_history(session, "worker", limit=1)] == ["v2"]


async def test_each_change_is_audited_and_unchanged_save_is_skipped(session: AsyncSession) -> None:
    await save_rules(session, "reviewer", "r1")
    await save_rules(session, "reviewer", "r1")
    events = await audit_store.list_events(session, limit=10, offset=0)
    assert [e.kind for e in events] == [AuditKind.RULES_CHANGED]
    assert events[0].payload is not None
    assert events[0].payload["key"] == "reviewer"
    assert len(await rule_history(session, "reviewer", limit=10)) == 1


@pytest.mark.parametrize("key", ["admin", "Worker", "", "../worker"])
async def test_unknown_key_is_rejected(session: AsyncSession, key: str) -> None:
    with pytest.raises(ValueError, match="unknown rules key"):
        await save_rules(session, key, "x")
    with pytest.raises(ValueError, match="unknown rules key"):
        await rule_history(session, key, limit=1)
