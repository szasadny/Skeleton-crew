from datetime import datetime

from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from skeleton_crew.core.db import Base
from skeleton_crew.domain.task_state import TaskKind, TaskState
from skeleton_crew.models.types import JsonStringList, StrEnumType, UtcDateTime, utc_now


class WorkerTask(Base):
    __tablename__ = "worker_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    issue_number: Mapped[int] = mapped_column(index=True)
    issue_title: Mapped[str] = mapped_column(String(256))
    state: Mapped[TaskState] = mapped_column(StrEnumType(TaskState), index=True)
    kind: Mapped[TaskKind] = mapped_column(
        StrEnumType(TaskKind), index=True, default=TaskKind.ISSUE, server_default=TaskKind.ISSUE
    )
    branch: Mapped[str | None] = mapped_column(String(255))
    pr_number: Mapped[int | None]
    base_sha: Mapped[str | None] = mapped_column(String(64))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    session_id: Mapped[str | None] = mapped_column(String(128))
    container_id: Mapped[str | None] = mapped_column(String(128))
    ci_rounds: Mapped[int] = mapped_column(default=0)
    review_rounds: Mapped[int] = mapped_column(default=0)
    policy_rounds: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str | None] = mapped_column(Text)
    blockers: Mapped[list[str] | None] = mapped_column(JsonStringList)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)
