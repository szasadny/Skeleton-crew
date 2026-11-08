from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web_helpers import Web, open_web

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.store import audit_store


@pytest.fixture
async def web(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Web]:
    async with open_web(tmp_path, sessionmaker) as harness:
        await harness.login()
        async with sessionmaker() as session:
            for i in range(60):
                await audit_store.record(
                    session, AuditKind.GITHUB_WRITE, AuditOutcome.DRY_RUN, f"write {i}"
                )
            await audit_store.record(session, AuditKind.POLICY_VIOLATION, AuditOutcome.BLOCKED, "x")
            await session.commit()
        yield harness


async def test_logs_lists_newest_first_with_pagination(web: Web) -> None:
    first = (await web.client.get("/logs")).text
    assert first.index("policy_violation</td>") < first.index("write 59")
    assert "write 9<" not in first
    assert 'href="/logs?page=2"' in first
    second = (await web.client.get("/logs?page=2")).text
    assert "write 0<" in second


async def test_logs_filter_by_kind(web: Web) -> None:
    html = (await web.client.get("/logs?kind=policy_violation")).text
    assert "write 1" not in html
    assert '<option value="policy_violation" selected>' in html
    assert "<td>x</td>" in html


async def test_unknown_kind_shows_all_events(web: Web) -> None:
    html = (await web.client.get("/logs?kind=bogus")).text
    assert "write 59" in html
    assert "<td>x</td>" in html


async def test_old_audit_path_redirects_to_logs(web: Web) -> None:
    response = await web.client.get("/audit?kind=policy_violation&page=2")
    assert response.status_code == 308
    assert response.headers["location"] == "/logs?kind=policy_violation&page=2"
    assert (await web.client.get("/audit")).headers["location"] == "/logs"
