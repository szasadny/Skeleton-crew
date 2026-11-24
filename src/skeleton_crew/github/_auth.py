import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from githubkit import AppAuthStrategy, AppInstallationAuthStrategy, GitHub
from githubkit_schemas.latest.types import AppPermissionsType

from skeleton_crew.core.clock import utc_now
from skeleton_crew.domain.repo_policy import REQUIRED_PERMISSIONS

TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
# Read-only contents: GitHub then rejects any attempt to land a PR with the API token.
API_TOKEN_PERMISSIONS = cast(AppPermissionsType, {**REQUIRED_PERMISSIONS, "contents": "read"})


class RepoTokenProvider:
    def __init__(
        self,
        app_client: GitHub[Any],
        installation_id: int,
        repo_name: str,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._app_client = app_client
        self._installation_id = installation_id
        self._repo_name = repo_name
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = datetime.min.replace(tzinfo=UTC)

    async def __call__(self) -> str:
        async with self._lock:
            if self._token is None or self._clock() >= self._expires_at - TOKEN_REFRESH_MARGIN:
                resp = await self._app_client.rest.apps.async_create_installation_access_token(
                    self._installation_id,
                    repositories=[self._repo_name],
                    permissions={"contents": "write"},
                )
                data = resp.json()
                self._token = str(data["token"])
                self._expires_at = datetime.fromisoformat(data["expires_at"])
            return self._token


async def make_github_client(
    app_id: int, private_key: str, installation_id: int | None, repo: str
) -> tuple[GitHub[Any], Callable[[], Awaitable[str]]]:
    owner, name = repo.split("/", 1)
    app_client = GitHub(AppAuthStrategy(app_id, private_key))
    if installation_id is None:
        resp = await app_client.rest.apps.async_get_repo_installation(owner, name)
        installation_id = int(resp.json()["id"])
    client = GitHub(
        AppInstallationAuthStrategy(
            app_id,
            private_key,
            installation_id,
            repositories=[name],
            permissions=API_TOKEN_PERMISSIONS,
        )
    )
    return client, RepoTokenProvider(app_client, installation_id, name)
