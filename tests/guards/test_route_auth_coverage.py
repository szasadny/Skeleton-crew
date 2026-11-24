import re
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.routing import BaseRoute, Mount
from web_helpers import Web, open_web

from skeleton_crew.web.security import LOGIN_PATH

# Reachable without a session. Every other route must refuse an anonymous request.
PUBLIC_PATHS = frozenset({LOGIN_PATH})
PUBLIC_MOUNTS = frozenset({"/static"})
_SAFE_METHODS = frozenset({"GET", "HEAD"})
_PATH_PARAM = re.compile(r"\{[^}]+\}")


@pytest.fixture
async def web(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Web]:
    async with open_web(tmp_path, sessionmaker) as harness:
        yield harness


def _flatten(routes: Sequence[BaseRoute]) -> Iterator[BaseRoute]:
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is None:
            yield route
        else:
            yield from _flatten(included.routes)


def _endpoints(web: Web) -> list[tuple[str, str]]:
    return sorted(
        (method, _PATH_PARAM.sub("1", route.path))
        for route in _flatten(web.app.routes)
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    )


def test_only_known_mounts_bypass_the_route_guard(web: Web) -> None:
    other = [route for route in _flatten(web.app.routes) if not isinstance(route, APIRoute)]
    assert all(isinstance(route, Mount) for route in other)
    assert {route.path for route in other if isinstance(route, Mount)} == PUBLIC_MOUNTS


async def test_every_route_refuses_an_anonymous_request(web: Web) -> None:
    endpoints = [(method, path) for method, path in _endpoints(web) if path not in PUBLIC_PATHS]
    assert endpoints
    for method, path in endpoints:
        response = await web.client.request(method, path)
        assert response.status_code == 303, (method, path)
        assert response.headers["location"] == LOGIN_PATH, (method, path)


async def test_every_unsafe_route_refuses_a_request_without_csrf_token(web: Web) -> None:
    await web.login()
    unsafe = [(method, path) for method, path in _endpoints(web) if method not in _SAFE_METHODS]
    assert unsafe
    for method, path in unsafe:
        response = await web.client.request(method, path)
        assert response.status_code == 403, (method, path)
