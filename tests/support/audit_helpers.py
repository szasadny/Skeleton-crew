from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.core.audit import AuditKind
from skeleton_crew.models.audit_event import AuditEvent
from skeleton_crew.store import audit_store

_LIMIT = 500


async def audit_events(session: AsyncSession, kind: AuditKind | None = None) -> list[AuditEvent]:
    return list(reversed(await audit_store.list_events(session, _LIMIT, 0, kind)))
