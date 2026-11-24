from datetime import UTC, datetime

import pytest

from skeleton_crew.domain.quota_policy import local_midnight, remaining


@pytest.mark.parametrize(
    ("now", "timezone", "expected"),
    [
        (datetime(2026, 3, 10, 12, tzinfo=UTC), "UTC", datetime(2026, 3, 10, tzinfo=UTC)),
        (
            datetime(2026, 3, 10, 23, 30, tzinfo=UTC),
            "Europe/Amsterdam",
            datetime(2026, 3, 10, 23, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 1, 1, tzinfo=UTC),
            "America/New_York",
            datetime(2026, 6, 30, 4, tzinfo=UTC),
        ),
    ],
)
def test_local_midnight_is_the_start_of_the_local_day_in_utc(
    now: datetime, timezone: str, expected: datetime
) -> None:
    assert local_midnight(now, timezone) == expected
    assert local_midnight(now, timezone).tzinfo is UTC


@pytest.mark.parametrize(
    ("limit", "used", "expected"), [(10, 3, 7), (10, 10, 0), (10, 12, 0), (0, 0, 0)]
)
def test_remaining_quota_never_goes_negative(limit: int, used: int, expected: int) -> None:
    assert remaining(limit, used) == expected
