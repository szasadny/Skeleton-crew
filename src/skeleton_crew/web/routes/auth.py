import asyncio
import math

from fastapi import APIRouter, Request
from fastapi.responses import Response

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.core.security import verify_password
from skeleton_crew.store import audit_store
from skeleton_crew.web.rendering import redirect, render
from skeleton_crew.web.security import LOGIN_PATH, client_ip, is_authenticated, log_in, log_out
from skeleton_crew.web.state import get_state

router = APIRouter()

_MAX_PASSWORD_CHARS = 1024


async def _audit(request: Request, outcome: AuditOutcome, summary: str, client: str) -> None:
    async with get_state(request).sessionmaker() as session:
        await audit_store.record(session, AuditKind.LOGIN, outcome, summary, {"client": client})
        await session.commit()


@router.get(LOGIN_PATH)
async def login_page(request: Request) -> Response:
    if is_authenticated(request):
        return redirect("/")
    return render(request, "login.html")


@router.post(LOGIN_PATH)
async def login(request: Request) -> Response:
    state = get_state(request)
    client = client_ip(request)
    wait_s = state.throttle.retry_after(client)
    if wait_s > 0:
        await _audit(request, AuditOutcome.BLOCKED, "login throttled", client)
        error = f"Too many failed attempts. Try again in {math.ceil(wait_s)} s."
        return render(request, "login.html", {"error": error}, 429)
    password = (await request.form()).get("password")
    valid = (
        isinstance(password, str)
        and len(password) <= _MAX_PASSWORD_CHARS
        and await asyncio.to_thread(
            verify_password, state.config.ui_password_hash.get_secret_value(), password
        )
    )
    if not valid:
        state.throttle.failed(client)
        await _audit(request, AuditOutcome.BLOCKED, "login failed", client)
        return render(request, "login.html", {"error": "Wrong password."}, 401)
    state.throttle.succeeded(client)
    log_in(request)
    await _audit(request, AuditOutcome.ALLOWED, "login succeeded", client)
    return redirect("/")


@router.post("/logout")
async def logout(request: Request) -> Response:
    log_out(request)
    return redirect(LOGIN_PATH)
