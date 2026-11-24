from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.status import HTTP_200_OK, HTTP_303_SEE_OTHER

from skeleton_crew.web.forms import label
from skeleton_crew.web.security import (
    CSRF_FIELD,
    CSRF_HEADER,
    LOGIN_PATH,
    csrf_token,
    is_authenticated,
)
from skeleton_crew.web.state import get_state

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _format_datetime(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M %Z") if value else ""


def _format_usd(value: float) -> str:
    return f"${value:.2f}"


templates.env.filters.update(dt=_format_datetime, usd=_format_usd, label=label)
templates.env.globals.update(csrf_field=CSRF_FIELD, csrf_header=CSRF_HEADER, login_path=LOGIN_PATH)


@dataclass(frozen=True)
class NavLink:
    href: str
    label: str
    prefix: str | None = None

    def is_current(self, path: str) -> bool:
        prefix = self.prefix or self.href
        return path == prefix if prefix == "/" else path.startswith(prefix)


@dataclass(frozen=True)
class NavGroup:
    title: str
    links: tuple[NavLink, ...]


NAV = (
    NavGroup(
        "Monitor",
        (
            NavLink("/", "Dashboard"),
            NavLink("/runs", "Runs"),
            NavLink("/blockers", "Blockers"),
            NavLink("/logs", "Logs"),
        ),
    ),
    NavGroup(
        "Configure",
        (
            NavLink("/repo", "Repo"),
            NavLink("/agents", "Agents"),
            NavLink("/rules/common", "Rules", prefix="/rules"),
            NavLink("/schedule", "Schedule"),
            NavLink("/limits", "Limits"),
            NavLink("/ci", "CI"),
        ),
    ),
)


class Message(StrEnum):
    SAVED = "saved"
    RUN_STARTED = "run_started"
    RUN_REFUSED = "run_refused"
    KILLED = "killed"
    RELEASED = "released"
    DRY_RUN_ON = "dry_run_on"
    DRY_RUN_OFF = "dry_run_off"
    RULES_RESET = "rules_reset"


MESSAGES: dict[str, str] = {
    Message.SAVED: "Saved.",
    Message.RUN_STARTED: "Run started.",
    Message.RUN_REFUSED: "No run started: a run is active or the kill switch is on.",
    Message.KILLED: "Kill switch engaged. Active work was stopped.",
    Message.RELEASED: "Kill switch released.",
    Message.DRY_RUN_ON: "Dry run enabled.",
    Message.DRY_RUN_OFF: "Dry run disabled. Runs will write to GitHub.",
    Message.RULES_RESET: "Rules reset to the default.",
}


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    status_code: int = HTTP_200_OK,
) -> Response:
    state = get_state(request)
    full_context: dict[str, Any] = {
        "csrf_token": csrf_token(request),
        "authenticated": is_authenticated(request),
        "running": state.controller.is_running,
        "repo": state.config.repo,
        "repo_url": state.config.repo_url,
        "nav": NAV,
        "path": request.url.path,
        "message": MESSAGES.get(request.query_params.get("msg", "")),
        **(context or {}),
    }
    return templates.TemplateResponse(request, name, full_context, status_code=status_code)


def redirect(url: str, message: Message | None = None) -> RedirectResponse:
    target = f"{url}?msg={message}" if message else url
    return RedirectResponse(target, status_code=HTTP_303_SEE_OTHER)
