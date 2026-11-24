import pytest
from web_helpers import Web

from skeleton_crew.core.audit import AuditEntry, AuditKind, AuditOutcome
from skeleton_crew.store import audit_store
from skeleton_crew.web.routes.logs import PAGE_SIZE

_WRITES = PAGE_SIZE + 10


@pytest.fixture
async def logged(web: Web) -> Web:
    async with web.sessionmaker() as session:
        for i in range(_WRITES):
            entry = AuditEntry(AuditKind.GITHUB_WRITE, AuditOutcome.DRY_RUN, f"write {i}")
            await audit_store.record(session, entry)
        blocked = AuditEntry(AuditKind.POLICY_VIOLATION, AuditOutcome.BLOCKED, "x")
        await audit_store.record(session, blocked)
        await session.commit()
    return web


async def test_logs_lists_newest_first_with_pagination(logged: Web) -> None:
    first = (await logged.client.get("/logs")).text
    assert first.index("policy_violation</td>") < first.index(f"write {_WRITES - 1}")
    assert f"write {_WRITES - PAGE_SIZE}<" not in first
    assert 'href="/logs?page=2"' in first
    second = (await logged.client.get("/logs?page=2")).text
    assert "write 0<" in second


async def test_logs_filter_by_kind(logged: Web) -> None:
    html = (await logged.client.get("/logs?kind=policy_violation")).text
    assert "write 1" not in html
    assert '<option value="policy_violation" selected>' in html
    assert "<td>x</td>" in html
    assert 'href="/logs?page=2' not in html


async def test_filtered_logs_keep_the_kind_when_paging(logged: Web) -> None:
    html = (await logged.client.get("/logs?kind=github_write")).text
    assert 'href="/logs?page=2&amp;kind=github_write"' in html


async def test_unknown_kind_shows_all_events(logged: Web) -> None:
    html = (await logged.client.get("/logs?kind=bogus")).text
    assert f"write {_WRITES - 1}" in html
    assert "<td>x</td>" in html


async def test_old_audit_path_is_gone(logged: Web) -> None:
    assert (await logged.client.get("/audit")).status_code == 404
