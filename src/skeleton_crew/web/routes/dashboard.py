from collections import Counter
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.store import audit_store, runs_store
from skeleton_crew.store.settings_store import load_settings, save_settings
from skeleton_crew.web.rendering import redirect, render
from skeleton_crew.web.state import get_state

router = APIRouter()


async def _status(request: Request) -> dict[str, Any]:
    state = get_state(request)
    async with state.sessionmaker() as session:
        settings = await load_settings(session)
        runs = await runs_store.list_runs(session, limit=1, offset=0)
        last_run = runs[0] if runs else None
        last_tasks = await runs_store.list_tasks(session, last_run.id) if last_run else []
        active = await runs_store.non_terminal_worker_tasks(session)
    return {
        "running": state.controller.is_running,
        "next_run": state.scheduler.next_run_time(),
        "settings": settings,
        "last_run": last_run,
        "last_run_states": sorted(Counter(task.state.value for task in last_tasks).items()),
        "last_run_blocked": sum(1 for task in last_tasks if task.blockers),
        "active_tasks": active,
        "repo": state.config.repo,
    }


@router.get("/")
async def dashboard(request: Request) -> Response:
    return render(request, "dashboard.html", await _status(request))


@router.get("/partials/status")
async def status_partial(request: Request) -> Response:
    return render(request, "_status.html", await _status(request))


@router.post("/actions/run-now")
async def run_now(request: Request) -> Response:
    run_id = await get_state(request).controller.start("manual")
    return redirect("/", "run_refused" if run_id is None else "run_started")


async def _set_kill_switch(request: Request, engaged: bool) -> None:
    async with get_state(request).sessionmaker() as session:
        settings = await load_settings(session)
        await save_settings(session, settings.model_copy(update={"kill_switch": engaged}))
        summary = "kill switch engaged from the UI" if engaged else "kill switch released"
        await audit_store.record(session, AuditKind.KILL_SWITCH, AuditOutcome.INFO, summary)
        await session.commit()


@router.post("/actions/kill")
async def kill(request: Request) -> Response:
    await _set_kill_switch(request, engaged=True)
    await get_state(request).controller.kill()
    return redirect("/", "killed")


@router.post("/actions/release")
async def release(request: Request) -> Response:
    await _set_kill_switch(request, engaged=False)
    return redirect("/", "released")


@router.post("/actions/dry-run")
async def dry_run(request: Request) -> Response:
    enabled = (await request.form()).get("enabled") == "on"
    async with get_state(request).sessionmaker() as session:
        settings = await load_settings(session)
        await save_settings(session, settings.model_copy(update={"dry_run": enabled}))
        await session.commit()
    return redirect("/", "dry_run_on" if enabled else "dry_run_off")
