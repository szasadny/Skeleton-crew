from collections.abc import Awaitable, Callable, Mapping

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException
from starlette.status import (
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_422_UNPROCESSABLE_CONTENT,
)

from skeleton_crew.domain.settings import InvalidRules
from skeleton_crew.services.errors import GENERAL, InvalidInput, NotFound
from skeleton_crew.web.rendering import redirect, render
from skeleton_crew.web.security import LOGIN_PATH, CsrfFailed, LoginRequired
from skeleton_crew.web.state import get_state

HX_REQUEST = "HX-Request"
HX_REDIRECT = "HX-Redirect"

type Errors = Mapping[str, list[str]]
type Rerender = Callable[[AsyncSession, Errors], Awaitable[Response]]


def on_invalid(request: Request, rerender: Rerender) -> None:
    request.state.rerender = rerender


def found[T](value: T | None) -> T:
    if value is None:
        raise NotFound
    return value


def _error_page(request: Request, status_code: int, title: str, detail: str) -> Response:
    return render(request, "error.html", {"title": title, "detail": detail}, status_code)


async def _login_required(request: Request, _exc: Exception) -> Response:
    if request.headers.get(HX_REQUEST) == "true":
        return Response(status_code=HTTP_401_UNAUTHORIZED, headers={HX_REDIRECT: LOGIN_PATH})
    return redirect(LOGIN_PATH)


async def _csrf_failed(request: Request, _exc: Exception) -> Response:
    return _error_page(request, HTTP_403_FORBIDDEN, "Forbidden", "Invalid or missing CSRF token.")


async def _not_found(request: Request, _exc: Exception) -> Response:
    return _error_page(request, HTTP_404_NOT_FOUND, "Not found", "Nothing lives at this address.")


async def _http_error(request: Request, exc: Exception) -> Response:
    if isinstance(exc, HTTPException) and exc.status_code != HTTP_404_NOT_FOUND:
        return await http_exception_handler(request, exc)
    return await _not_found(request, exc)


async def _invalid_input(request: Request, exc: Exception) -> Response:
    errors = exc.errors if isinstance(exc, InvalidInput) else {GENERAL: [str(exc)]}
    rerender: Rerender = request.state.rerender
    async with get_state(request).sessionmaker() as session:
        response = await rerender(session, errors)
    response.status_code = HTTP_422_UNPROCESSABLE_CONTENT
    return response


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(LoginRequired, _login_required)
    app.add_exception_handler(CsrfFailed, _csrf_failed)
    app.add_exception_handler(NotFound, _not_found)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(InvalidInput, _invalid_input)
    app.add_exception_handler(InvalidRules, _invalid_input)
