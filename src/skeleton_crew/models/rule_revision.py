from datetime import datetime

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from skeleton_crew.core.db import Base
from skeleton_crew.models.types import UtcDateTime, utc_now


class RuleRevision(Base):
    __tablename__ = "rule_revisions"
    __table_args__ = (Index("ix_rule_revisions_role_id", "role", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
