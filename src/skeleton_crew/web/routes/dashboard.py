from fastapi import APIRouter, Request
from fastapi.responses import Response

from skeleton_crew.domain.task_state import RunTrigger
from skeleton_crew.services import settings_service
from skeleton_crew.web.forms import form_value
from skeleton_crew.web.rendering import Message, redirect, render
from skeleton_crew.web.state import SessionDep, get_state
from skeleton_crew.web.views.dashboard import status_context

router = APIRouter()

STATUS_POLL_S = 5


@router.get("/")
async def dashboard(request: Request, session: SessionDep) -> Response:
    context = await status_context(get_state(request), session)
    return render(request, "dashboard.html", {**context, "poll_s": STATUS_POLL_S})


@router.get("/partials/status")
async def status_partial(request: Request, session: SessionDep) -> Response:
    context = await status_context(get_state(request), session)
    return render(request, "_status.html", {**context, "oob": True})


@router.post("/actions/run-now")
async def run_now(request: Request) -> Response:
    run_id = await get_state(request).controller.start(RunTrigger.MANUAL)
    return redirect("/", Message.RUN_REFUSED if run_id is None else Message.RUN_STARTED)


@router.post("/actions/kill")
async def kill(request: Request, session: SessionDep) -> Response:
    await settings_service.engage_kill_switch(session, get_state(request).controller.kill)
    return redirect("/", Message.KILLED)


@router.post("/actions/release")
async def release(session: SessionDep) -> Response:
    await settings_service.set_flags(session, kill_switch=False)
    return redirect("/", Message.RELEASED)


@router.post("/actions/dry-run")
async def dry_run(request: Request, session: SessionDep) -> Response:
    enabled = form_value(await request.form(), "enabled") == "on"
    await settings_service.set_flags(session, dry_run=enabled)
    return redirect("/", Message.DRY_RUN_ON if enabled else Message.DRY_RUN_OFF)
