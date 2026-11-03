from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, Dialect, String
from sqlalchemy.types import TypeDecorator

ENUM_LENGTH = 32


def utc_now() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    # SQLite drops tzinfo, so values are stored as naive UTC and re-tagged on load.
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class StrEnumType[E: StrEnum](TypeDecorator[E]):
    impl = String(ENUM_LENGTH)
    cache_ok = True

    def __init__(self, enum_cls: type[E]) -> None:
        super().__init__()
        self._enum_cls = enum_cls

    def process_bind_param(self, value: E | str | None, dialect: Dialect) -> str | None:
        return None if value is None else self._enum_cls(value).value

    def process_result_value(self, value: str | None, dialect: Dialect) -> E | None:
        return None if value is None else self._enum_cls(value)
