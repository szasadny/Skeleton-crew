from datetime import datetime

from sqlalchemy import CheckConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from skeleton_crew.core.db import Base
from skeleton_crew.models.types import UtcDateTime, utc_now

SETTINGS_ROW_ID = 1


class SettingsRow(Base):
    __tablename__ = "settings"
    __table_args__ = (CheckConstraint(f"id = {SETTINGS_ROW_ID}", name="ck_settings_single_row"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    data: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)
