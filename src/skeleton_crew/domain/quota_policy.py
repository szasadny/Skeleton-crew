from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def local_midnight(now: datetime, timezone: str) -> datetime:
    local = now.astimezone(ZoneInfo(timezone))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def remaining(limit: int, used: int) -> int:
    return max(limit - used, 0)
