from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from skeleton_crew.domain.settings import AppSettings, Weekday

_JOB_ID = "scheduled-run"
_MISFIRE_GRACE_S = 3600
_WEEKDAY_ORDER = tuple(Weekday)


def cron_trigger(settings: AppSettings) -> CronTrigger | None:
    schedule = settings.schedule
    if not schedule.weekdays:
        return None
    days = ",".join(day.value for day in _WEEKDAY_ORDER if day in schedule.weekdays)
    return CronTrigger(
        day_of_week=days,
        hour=schedule.start_time.hour,
        minute=schedule.start_time.minute,
        timezone=ZoneInfo(schedule.timezone),
    )


class RunScheduler:
    def __init__(self, trigger: Callable[[], Awaitable[object]]) -> None:
        self._trigger = trigger
        self._scheduler = AsyncIOScheduler()
        self._cron: CronTrigger | None = None

    async def _fire(self) -> None:
        await self._trigger()

    def start(self, settings: AppSettings) -> None:
        self.apply(settings)
        self._scheduler.start()

    def apply(self, settings: AppSettings) -> None:
        self._scheduler.remove_all_jobs()
        self._cron = cron_trigger(settings)
        if self._cron is None:
            return
        self._scheduler.add_job(
            self._fire,
            trigger=self._cron,
            id=_JOB_ID,
            misfire_grace_time=_MISFIRE_GRACE_S,
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    def next_run_time(self, after: datetime | None = None) -> datetime | None:
        if self._cron is None:
            return None
        now = after or datetime.now(self._cron.timezone)
        fire_time: datetime | None = self._cron.get_next_fire_time(None, now)
        return fire_time
