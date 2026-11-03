from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.settings import RULE_KEYS
from skeleton_crew.models.rule_revision import RuleRevision
from skeleton_crew.store import audit_store


def _check_key(key: str) -> None:
    if key not in RULE_KEYS:
        raise ValueError(f"unknown rules key: {key}")


async def active_rules(session: AsyncSession, key: str) -> str | None:
    _check_key(key)
    query = (
        select(RuleRevision.content)
        .where(RuleRevision.role == key)
        .order_by(RuleRevision.id.desc())
        .limit(1)
    )
    return await session.scalar(query)


async def save_rules(session: AsyncSession, key: str, content: str) -> None:
    if await active_rules(session, key) == content:
        return
    revision = RuleRevision(role=key, content=content)
    session.add(revision)
    await session.flush()
    await audit_store.record(
        session,
        AuditKind.RULES_CHANGED,
        AuditOutcome.INFO,
        f"rules changed: {key}",
        {"key": key, "revision_id": revision.id},
    )


async def rule_history(session: AsyncSession, key: str, limit: int) -> list[RuleRevision]:
    _check_key(key)
    query = (
        select(RuleRevision)
        .where(RuleRevision.role == key)
        .order_by(RuleRevision.id.desc())
        .limit(limit)
    )
    return list(await session.scalars(query))
