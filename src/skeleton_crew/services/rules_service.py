from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.agents.rules import default_rules
from skeleton_crew.agents.rules import effective_rules as effective_rules
from skeleton_crew.core.audit import AuditEntry, AuditKind, AuditOutcome
from skeleton_crew.domain.settings import check_rule_key
from skeleton_crew.models.rule_revision import RuleRevision
from skeleton_crew.services.errors import NotFound
from skeleton_crew.store import audit_store, rules_store

HISTORY_LIMIT = 20


@dataclass(frozen=True)
class RulesEditor:
    content: str
    is_default: bool
    history: list[RuleRevision]


def _known(key: str) -> None:
    try:
        check_rule_key(key)
    except ValueError:
        raise NotFound(key) from None


async def editor(session: AsyncSession, key: str) -> RulesEditor:
    _known(key)
    active = await rules_store.active_rules(session, key)
    history = await rules_store.rule_history(session, key, HISTORY_LIMIT)
    return RulesEditor(effective_rules(key, active), active is None, history)


async def save_rules(session: AsyncSession, key: str, content: str) -> None:
    _known(key)
    revision = await rules_store.save_rules(session, key, content)
    if revision is not None:
        await audit_store.record(
            session,
            AuditEntry(
                AuditKind.RULES_CHANGED,
                AuditOutcome.INFO,
                f"rules changed: {key}",
                {"key": key, "revision_id": revision.id},
            ),
        )
    await session.commit()


async def reset_rules(session: AsyncSession, key: str) -> None:
    _known(key)
    await save_rules(session, key, default_rules(key))


async def revision(session: AsyncSession, key: str, revision_id: int) -> RuleRevision:
    _known(key)
    found = await rules_store.get_revision(session, key, revision_id)
    if found is None:
        raise NotFound(f"{key} revision {revision_id}")
    return found
