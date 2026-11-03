from datetime import datetime

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from skeleton_crew.core.db import Base
from skeleton_crew.domain.task_state import RunState
from skeleton_crew.models.types import StrEnumType, UtcDateTime, utc_now


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16))
    dry_run: Mapped[bool]
    state: Mapped[RunState] = mapped_column(StrEnumType(RunState))
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    note: Mapped[str | None] = mapped_column(Text)
