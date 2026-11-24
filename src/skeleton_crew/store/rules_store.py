from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.core.audit import AuditEntry, AuditKind, AuditOutcome
from skeleton_crew.domain.settings import check_rule_key, check_rules_content
from skeleton_crew.models.rule_revision import RuleRevision
from skeleton_crew.store import audit_store


async def active_rules(session: AsyncSession, key: str) -> str | None:
    check_rule_key(key)
    query = (
        select(RuleRevision.content)
        .where(RuleRevision.role == key)
        .order_by(RuleRevision.id.desc())
        .limit(1)
    )
    return await session.scalar(query)


async def save_rules(session: AsyncSession, key: str, content: str) -> None:
    check_rule_key(key)
    check_rules_content(content)
    if await active_rules(session, key) == content:
        return
    revision = RuleRevision(role=key, content=content)
    session.add(revision)
    await session.flush()
    await audit_store.record(
        session,
        AuditEntry(
            AuditKind.RULES_CHANGED,
            AuditOutcome.INFO,
            f"rules changed: {key}",
            {"key": key, "revision_id": revision.id},
        ),
    )


async def rule_history(session: AsyncSession, key: str, limit: int) -> list[RuleRevision]:
    check_rule_key(key)
    query = (
        select(RuleRevision)
        .where(RuleRevision.role == key)
        .order_by(RuleRevision.id.desc())
        .limit(limit)
    )
    return list(await session.scalars(query))


async def get_revision(session: AsyncSession, key: str, revision_id: int) -> RuleRevision | None:
    check_rule_key(key)
    query = select(RuleRevision).where(RuleRevision.role == key, RuleRevision.id == revision_id)
    return await session.scalar(query)
