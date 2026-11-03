import asyncio
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from skeleton_crew.domain.settings import AppSettings, Schedule, Weekday
from skeleton_crew.scheduler.scheduler import RunScheduler, cron_trigger

_AMSTERDAM = ZoneInfo("Europe/Amsterdam")


def _settings(*days: Weekday, at: time = time(1, 0), tz: str = "Europe/Amsterdam") -> AppSettings:
    return AppSettings(schedule=Schedule(weekdays=frozenset(days), start_time=at, timezone=tz))


async def _noop() -> None:
    return None


def test_no_weekdays_means_no_trigger_and_no_next_run() -> None:
    scheduler = RunScheduler(_noop)
    scheduler.apply(_settings())
    assert cron_trigger(_settings()) is None
    assert scheduler.next_run_time() is None


def test_next_run_is_the_next_selected_weekday_in_the_schedule_timezone() -> None:
    scheduler = RunScheduler(_noop)
    scheduler.apply(_settings(Weekday.MON, Weekday.THU, at=time(1, 30)))
    wednesday_noon = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    assert scheduler.next_run_time(wednesday_noon) == datetime(
        2026, 10, 29, 1, 30, tzinfo=_AMSTERDAM
    )
    thursday_late = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
    assert scheduler.next_run_time(thursday_late) == datetime(2026, 11, 2, 1, 30, tzinfo=_AMSTERDAM)


def test_timezone_is_respected() -> None:
    scheduler = RunScheduler(_noop)
    scheduler.apply(_settings(Weekday.FRI, at=time(9, 0), tz="America/New_York"))
    next_run = scheduler.next_run_time(datetime(2026, 10, 30, 12, 0, tzinfo=UTC))
    assert next_run is not None
    assert next_run.astimezone(UTC) == datetime(2026, 10, 30, 13, 0, tzinfo=UTC)


def test_apply_replaces_and_clears_the_schedule() -> None:
    scheduler = RunScheduler(_noop)
    scheduler.apply(_settings(Weekday.MON))
    scheduler.apply(_settings(Weekday.SUN, at=time(6, 0)))
    next_run = scheduler.next_run_time(datetime(2026, 10, 28, tzinfo=UTC))
    assert next_run is not None
    assert next_run.weekday() == 6
    scheduler.apply(_settings())
    assert scheduler.next_run_time() is None


async def test_started_job_uses_safe_options_and_fires_the_trigger() -> None:
    calls: list[str] = []

    async def trigger() -> None:
        calls.append("run")

    scheduler = RunScheduler(trigger)
    scheduler.start(_settings(Weekday.TUE))
    assert scheduler.running
    jobs = scheduler._scheduler.get_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert (job.misfire_grace_time, job.coalesce, job.max_instances) == (3600, True, 1)
    await job.func()
    assert calls == ["run"]
    scheduler.apply(_settings())
    assert scheduler._scheduler.get_jobs() == []
    scheduler.shutdown()
    await asyncio.sleep(0)
    assert not scheduler.running
    scheduler.shutdown()
