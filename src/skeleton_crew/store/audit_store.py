from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.core.audit import AuditEntry, AuditKind
from skeleton_crew.models.audit_event import AUDIT_SUMMARY_LENGTH, AuditEvent

_ELLIPSIS = "…"


def _cap(summary: str) -> str:
    if len(summary) <= AUDIT_SUMMARY_LENGTH:
        return summary
    return summary[: AUDIT_SUMMARY_LENGTH - len(_ELLIPSIS)] + _ELLIPSIS


async def record(session: AsyncSession, entry: AuditEntry) -> None:
    session.add(
        AuditEvent(
            kind=entry.kind,
            outcome=entry.outcome,
            summary=_cap(entry.summary),
            payload=None if entry.payload is None else dict(entry.payload),
            run_id=entry.run_id,
        )
    )
    await session.flush()


async def list_events(
    session: AsyncSession, limit: int, offset: int, kind: AuditKind | None = None
) -> list[AuditEvent]:
    query = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit).offset(offset)
    if kind is not None:
        query = query.where(AuditEvent.kind == kind)
    return list(await session.scalars(query))
