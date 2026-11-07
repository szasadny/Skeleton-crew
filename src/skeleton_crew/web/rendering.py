from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from skeleton_crew.web.security import csrf_token, is_authenticated
from skeleton_crew.web.state import get_state

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _format_datetime(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M %Z") if value else ""


templates.env.filters["dt"] = _format_datetime

NAV = (
    ("/", "Dashboard", "/"),
    ("/schedule", "Schedule", "/schedule"),
    ("/limits", "Limits", "/limits"),
    ("/agents", "Agents", "/agents"),
    ("/repo", "Repo", "/repo"),
    ("/ci", "CI", "/ci"),
    ("/rules/common", "Rules", "/rules"),
    ("/runs", "Runs", "/runs"),
    ("/audit", "Audit", "/audit"),
)

MESSAGES = {
    "saved": "Saved.",
    "run_started": "Run started.",
    "run_refused": "No run started: a run is active or the kill switch is on.",
    "killed": "Kill switch engaged. Active work was stopped.",
    "released": "Kill switch released.",
    "dry_run_on": "Dry run enabled.",
    "dry_run_off": "Dry run disabled. Runs will write to GitHub.",
    "rules_reset": "Rules reset to the default.",
}


def render(
    request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200
) -> Response:
    state = get_state(request)
    secret = state.config.session_secret.get_secret_value()
    message_key = request.query_params.get("msg", "")
    full_context: dict[str, Any] = {
        "csrf_token": csrf_token(request, secret),
        "authenticated": is_authenticated(request),
        "running": state.controller.is_running,
        "repo": state.config.repo,
        "nav": NAV,
        "path": request.url.path,
        "message": MESSAGES.get(message_key),
        **(context or {}),
    }
    return templates.TemplateResponse(request, name, full_context, status_code=status_code)


def redirect(url: str, message: str | None = None) -> RedirectResponse:
    target = f"{url}?msg={message}" if message else url
    return RedirectResponse(target, status_code=303)
