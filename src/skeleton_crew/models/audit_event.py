from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.core.db import Base
from skeleton_crew.models.types import StrEnumType, UtcDateTime, utc_now

AUDIT_SUMMARY_LENGTH = 500


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, index=True)
    kind: Mapped[AuditKind] = mapped_column(StrEnumType(AuditKind), index=True)
    outcome: Mapped[AuditOutcome] = mapped_column(StrEnumType(AuditOutcome))
    summary: Mapped[str] = mapped_column(String(AUDIT_SUMMARY_LENGTH))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), index=True)
