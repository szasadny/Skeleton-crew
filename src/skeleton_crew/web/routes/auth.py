import math

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_429_TOO_MANY_REQUESTS

from skeleton_crew.services.login_service import LoginOutcome
from skeleton_crew.web.forms import form_value
from skeleton_crew.web.rendering import redirect, render
from skeleton_crew.web.security import LOGIN_PATH, client_ip, is_authenticated, log_in, log_out
from skeleton_crew.web.state import SessionDep, get_state

router = APIRouter()


@router.get(LOGIN_PATH)
async def login_page(request: Request) -> Response:
    if is_authenticated(request):
        return redirect("/")
    return render(request, "login.html")


@router.post(LOGIN_PATH)
async def login(request: Request, session: SessionDep) -> Response:
    password = form_value(await request.form(), "password")
    result = await get_state(request).logins.attempt(session, client_ip(request), password)
    if result.outcome is LoginOutcome.THROTTLED:
        alert = f"Too many failed attempts. Try again in {math.ceil(result.retry_after_s)} s."
        return render(request, "login.html", {"alerts": [alert]}, HTTP_429_TOO_MANY_REQUESTS)
    if result.outcome is LoginOutcome.FAILED:
        return render(request, "login.html", {"alerts": ["Wrong password."]}, HTTP_401_UNAUTHORIZED)
    log_in(request)
    return redirect("/")


@router.post("/logout")
async def logout(request: Request) -> Response:
    log_out(request)
    return redirect(LOGIN_PATH)
