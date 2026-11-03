from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.models.audit_event import AUDIT_SUMMARY_LENGTH, AuditEvent

_ELLIPSIS = "…"


def _cap(summary: str) -> str:
    if len(summary) <= AUDIT_SUMMARY_LENGTH:
        return summary
    return summary[: AUDIT_SUMMARY_LENGTH - len(_ELLIPSIS)] + _ELLIPSIS


async def record(
    session: AsyncSession,
    kind: AuditKind,
    outcome: AuditOutcome,
    summary: str,
    payload: Mapping[str, object] | None = None,
    run_id: int | None = None,
) -> None:
    session.add(
        AuditEvent(
            kind=kind,
            outcome=outcome,
            summary=_cap(summary),
            payload=None if payload is None else dict(payload),
            run_id=run_id,
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
