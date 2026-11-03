from zoneinfo import available_timezones

from fastapi import APIRouter, Request
from fastapi.responses import Response

from skeleton_crew.domain.settings import AppSettings, Schedule, Weekday
from skeleton_crew.web.forms import Kind, SettingsPage, number_specs
from skeleton_crew.web.state import get_state

router = APIRouter()


def _apply_schedule(request: Request, settings: AppSettings) -> None:
    get_state(request).scheduler.apply(settings)


_PAGE = SettingsPage(
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
    on_saved=_apply_schedule,
)


@router.get("/schedule")
async def show(request: Request) -> Response:
    return await _PAGE.show(request)


@router.post("/schedule")
async def submit(request: Request) -> Response:
    return await _PAGE.submit(request)
