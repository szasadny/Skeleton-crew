from zoneinfo import available_timezones

from fastapi import APIRouter

from skeleton_crew.domain.settings import AppSettings, Schedule, Weekday
from skeleton_crew.web.forms import Kind, number_specs
from skeleton_crew.web.settings_page import PageHooks, SettingsPage
from skeleton_crew.web.state import WebState

router = APIRouter()


def _apply_schedule(state: WebState, settings: AppSettings) -> None:
    state.scheduler.apply(settings)


SettingsPage(
    template="schedule.html",
    fields={
        "schedule.weekdays": Kind.MULTI,
        "schedule.start_time": Kind.TEXT,
        "schedule.window_hours": Kind.NUMBER,
        "schedule.timezone": Kind.TEXT,
    },
    context={
        "weekdays": tuple(Weekday),
        "window": number_specs("schedule", Schedule, ["window_hours"])[0],
        "timezones": sorted(available_timezones()),
    },
    hooks=PageHooks(on_saved=_apply_schedule),
).include(router, "/schedule")
