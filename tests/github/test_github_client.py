import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from githubkit import AppAuthStrategy, GitHub
from githubkit.auth.app import AppAuth

from skeleton_crew.github.gateway import (
    API_TOKEN_PERMISSIONS,
    RepoTokenProvider,
    make_github_client,
)

API = "https://api.github.com"
FAKE_KEY = "not-a-real-key"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fake_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AppAuth, "_create_jwt", lambda self: "app-jwt")


def _token_response(token: str, expires: datetime) -> httpx.Response:
    return httpx.Response(
        201, json={"token": token, "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}
    )


@respx.mock
async def test_make_github_client_resolves_installation_with_app_jwt() -> None:
    lookup = respx.get(f"{API}/repos/o/r/installation").mock(
        return_value=httpx.Response(200, json={"id": 77, "permissions": {}})
    )
    mint = respx.post(f"{API}/app/installations/77/access_tokens").mock(
        return_value=_token_response("git-token-1", datetime.now(UTC) + timedelta(hours=1))
    )

    _, provider = await make_github_client(1, FAKE_KEY, None, "o/r")
    minted = await provider()

    assert lookup.calls[0].request.headers["authorization"] == "Bearer app-jwt"
    assert minted == "git-token-1"
    body = json.loads(mint.calls[0].request.content)
    assert body == {"repositories": ["r"], "permissions": {"contents": "write"}}
    assert mint.calls[0].request.headers["authorization"] == "Bearer app-jwt"


@respx.mock(assert_all_mocked=True)
async def test_make_github_client_skips_lookup_with_known_installation() -> None:
    client, _ = await make_github_client(1, FAKE_KEY, 77, "o/r")

    assert client is not None
    assert not respx.calls


@respx.mock
async def test_token_is_cached_until_five_minutes_before_expiry() -> None:
    now = [NOW]
    mint = respx.post(f"{API}/app/installations/77/access_tokens").mock(
        side_effect=[
            _token_response("t1", NOW + timedelta(hours=1)),
            _token_response("t2", NOW + timedelta(hours=2)),
        ]
    )
    app = GitHub(AppAuthStrategy(1, FAKE_KEY), http_cache=False, auto_retry=False)
    provider = RepoTokenProvider(app, 77, "r", clock=lambda: now[0])

    assert await provider() == "t1"
    now[0] = NOW + timedelta(minutes=54)
    assert await provider() == "t1"
    now[0] = NOW + timedelta(minutes=55)
    assert await provider() == "t2"
    assert mint.call_count == 2


async def test_api_token_cannot_write_contents_so_github_refuses_merges() -> None:
    client, _ = await make_github_client(1, FAKE_KEY, 77, "o/r")

    assert client.auth.permissions == API_TOKEN_PERMISSIONS
    assert API_TOKEN_PERMISSIONS["contents"] == "read"
