from datetime import datetime

from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from skeleton_crew.core.db import Base
from skeleton_crew.domain.task_state import RefineState
from skeleton_crew.models.types import StrEnumType, UtcDateTime, utc_now


class RefineTask(Base):
    __tablename__ = "refine_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    issue_number: Mapped[int] = mapped_column(index=True)
    state: Mapped[RefineState] = mapped_column(StrEnumType(RefineState), index=True)
    session_id: Mapped[str | None] = mapped_column(String(128))
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)
