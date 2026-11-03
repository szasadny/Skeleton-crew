from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web_helpers import Web, open_web

from skeleton_crew.agents.rules import default_rules
from skeleton_crew.core.audit import AuditKind
from skeleton_crew.domain.settings import RULE_KEYS
from skeleton_crew.store import audit_store, rules_store


@pytest.fixture
async def web(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Web]:
    async with open_web(tmp_path, sessionmaker) as harness:
        await harness.login()
        yield harness


async def _active(web: Web, key: str) -> str | None:
    async with web.sessionmaker() as session:
        return await rules_store.active_rules(session, key)


@pytest.mark.parametrize("key", RULE_KEYS)
async def test_editor_shows_default_rules_before_any_save(web: Web, key: str) -> None:
    html = (await web.client.get(f"/rules/{key}")).text
    assert "Showing the default rules" in html
    first_line = default_rules(key).strip().splitlines()[0]
    assert first_line.replace("&", "&amp;").replace("<", "&lt;") in html


@pytest.mark.parametrize("path", ["/rules/admin", "/rules/%2E%2E", "/rules/common/revisions/abc"])
async def test_unknown_rules_key_is_404(web: Web, path: str) -> None:
    assert (await web.client.get(path)).status_code in {404, 422}


async def test_post_to_unknown_rules_key_is_404(web: Web) -> None:
    assert (await web.post("/rules/admin", {"content": "x"})).status_code == 404
    assert (await web.post("/rules/admin/reset")).status_code == 404


async def test_save_appends_revision_and_audits(web: Web) -> None:
    response = await web.post("/rules/worker", {"content": "Be careful.\r\nTest first."})
    assert response.headers["location"] == "/rules/worker?msg=saved"
    assert await _active(web, "worker") == "Be careful.\nTest first."
    await web.post("/rules/worker", {"content": "Second version."})
    html = (await web.client.get("/rules/worker")).text
    assert "Second version." in html
    assert html.count("/rules/worker/revisions/") == 2
    async with web.sessionmaker() as session:
        events = await audit_store.list_events(session, 10, 0, AuditKind.RULES_CHANGED)
    assert [event.summary for event in events] == ["rules changed: worker"] * 2


async def test_view_one_revision(web: Web) -> None:
    await web.post("/rules/reviewer", {"content": "Old <script>alert(1)</script>"})
    await web.post("/rules/reviewer", {"content": "New"})
    async with web.sessionmaker() as session:
        oldest = (await rules_store.rule_history(session, "reviewer", 20))[-1]
    html = (await web.client.get(f"/rules/reviewer/revisions/{oldest.id}")).text
    assert "Old &lt;script&gt;alert(1)&lt;/script&gt;" in html
    other = await web.client.get(f"/rules/worker/revisions/{oldest.id}")
    assert other.status_code == 404


async def test_too_long_rules_are_rejected(web: Web) -> None:
    response = await web.post("/rules/common", {"content": "x" * 20_001})
    assert response.status_code == 422
    assert "limited to 20000 characters" in response.text
    assert await _active(web, "common") is None


async def test_empty_rules_are_rejected(web: Web) -> None:
    assert (await web.post("/rules/common", {"content": "  \r\n"})).status_code == 422
    assert await _active(web, "common") is None


async def test_reset_saves_the_default_as_new_revision(web: Web) -> None:
    await web.post("/rules/selector", {"content": "Custom"})
    response = await web.post("/rules/selector/reset")
    assert response.headers["location"] == "/rules/selector?msg=rules_reset"
    assert await _active(web, "selector") == default_rules("selector")
    async with web.sessionmaker() as session:
        assert len(await rules_store.rule_history(session, "selector", 20)) == 2
