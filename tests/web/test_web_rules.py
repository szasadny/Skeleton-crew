import pytest
from web_helpers import NOTICE_CLASS, SAVE_BUTTON, Web, notice

from skeleton_crew.agents.rules import default_rules
from skeleton_crew.core.audit import AuditKind
from skeleton_crew.domain.settings import MAX_RULES_CHARS, RULE_KEYS
from skeleton_crew.services.rules_service import HISTORY_LIMIT
from skeleton_crew.store import rules_store


async def _active(web: Web, key: str) -> str | None:
    async with web.sessionmaker() as session:
        return await rules_store.active_rules(session, key)


async def _oldest_id(web: Web, key: str) -> int:
    async with web.sessionmaker() as session:
        return (await rules_store.rule_history(session, key, HISTORY_LIMIT * 2))[-1].id


@pytest.mark.parametrize("key", RULE_KEYS)
async def test_editor_shows_default_rules_before_any_save(web: Web, key: str) -> None:
    html = (await web.client.get(f"/rules/{key}")).text
    assert 'data-state="default"' in html
    assert "Nothing saved yet" in html
    first_line = default_rules(key).strip().splitlines()[0]
    assert first_line.replace("&", "&amp;").replace("<", "&lt;") in html


@pytest.mark.parametrize("path", ["/rules/admin", "/rules/%2E%2E", "/rules/common/revisions/abc"])
async def test_unknown_rules_key_is_404(web: Web, path: str) -> None:
    assert (await web.client.get(path)).status_code in {404, 422}


async def test_unknown_rules_key_is_an_html_404(web: Web) -> None:
    response = await web.client.get("/rules/admin")
    assert response.status_code == 404
    assert "<h1>Not found</h1>" in response.text


async def test_post_to_unknown_rules_key_is_404(web: Web) -> None:
    assert (await web.post("/rules/admin", {"content": "x"})).status_code == 404
    assert (await web.post("/rules/admin", {"content": ""})).status_code == 404
    assert (await web.post("/rules/admin/reset")).status_code == 404


async def test_save_appends_revision_and_audits(web: Web) -> None:
    response = await web.post("/rules/worker", {"content": "Be careful.\r\nTest first."})
    assert response.headers["location"] == "/rules/worker?msg=saved"
    assert await _active(web, "worker") == "Be careful.\nTest first."
    await web.post("/rules/worker", {"content": "Second version."})
    html = (await web.client.get("/rules/worker")).text
    assert "Second version." in html
    assert html.count("/rules/worker/revisions/") == 2
    assert 'data-state="saved"' in html
    assert "Nothing saved yet" not in html
    events = await web.audit(AuditKind.RULES_CHANGED)
    assert [event.summary for event in events] == ["rules changed: worker"] * 2


async def test_saved_rules_keep_surrounding_whitespace(web: Web) -> None:
    await web.post("/rules/worker", {"content": "  Indented.\n"})
    assert await _active(web, "worker") == "  Indented.\n"


async def test_saving_the_same_rules_twice_adds_no_revision(web: Web) -> None:
    await web.post("/rules/worker", {"content": "Same."})
    await web.post("/rules/worker", {"content": "Same."})
    assert len(await web.audit(AuditKind.RULES_CHANGED)) == 1


async def test_view_one_revision(web: Web) -> None:
    await web.post("/rules/reviewer", {"content": "Old <script>alert(1)</script>"})
    await web.post("/rules/reviewer", {"content": "New"})
    oldest = await _oldest_id(web, "reviewer")
    html = (await web.client.get(f"/rules/reviewer/revisions/{oldest}")).text
    assert "Old &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert (await web.client.get(f"/rules/worker/revisions/{oldest}")).status_code == 404


async def test_revision_older_than_the_history_page_opens(web: Web) -> None:
    await web.post("/rules/worker", {"content": "Oldest rules"})
    for n in range(HISTORY_LIMIT + 5):
        await web.post("/rules/worker", {"content": f"Version {n}"})
    oldest = await _oldest_id(web, "worker")
    html = (await web.client.get("/rules/worker")).text
    assert f'/rules/worker/revisions/{oldest}"' not in html
    view = await web.client.get(f"/rules/worker/revisions/{oldest}")
    assert view.status_code == 200
    assert "Oldest rules" in view.text


async def test_too_long_rules_are_rejected(web: Web) -> None:
    response = await web.post("/rules/common", {"content": "x" * (MAX_RULES_CHARS + 1)})
    assert response.status_code == 422
    assert f"limited to {MAX_RULES_CHARS} characters" in response.text
    assert await _active(web, "common") is None


async def test_empty_rules_are_rejected(web: Web) -> None:
    response = await web.post("/rules/common", {"content": "  \r\n"})
    assert response.status_code == 422
    assert '<div class="alert"><p>Rules cannot be empty.</p></div>' in response.text
    assert await _active(web, "common") is None


async def test_rejected_rules_stay_in_the_editor(web: Web) -> None:
    await web.post("/rules/common", {"content": "Saved before."})
    response = await web.post("/rules/common", {"content": "x" * (MAX_RULES_CHARS + 1)})
    assert "x" * (MAX_RULES_CHARS + 1) in response.text
    assert 'data-state="saved"' in response.text


async def test_reset_saves_the_default_as_new_revision(web: Web) -> None:
    await web.post("/rules/selector", {"content": "Custom"})
    response = await web.post("/rules/selector/reset")
    assert response.headers["location"] == "/rules/selector?msg=rules_reset"
    assert await _active(web, "selector") == default_rules("selector")
    async with web.sessionmaker() as session:
        assert len(await rules_store.rule_history(session, "selector", HISTORY_LIMIT)) == 2


@pytest.mark.parametrize(
    ("msg", "text"), [("", ""), ("saved", "Saved."), ("rules_reset", "Rules reset to the default.")]
)
async def test_rules_message_sits_left_of_save_button(web: Web, msg: str, text: str) -> None:
    html = (await web.client.get(f"/rules/common?msg={msg}")).text
    assert html.count(NOTICE_CLASS) == 1
    message_at = html.index(notice(text))
    assert message_at < html.index(SAVE_BUTTON)
