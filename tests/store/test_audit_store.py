from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.models.audit_event import AUDIT_SUMMARY_LENGTH
from skeleton_crew.store.audit_store import list_events, record
from skeleton_crew.store.runs_store import create_run


async def test_events_are_listed_newest_first_with_paging(session: AsyncSession) -> None:
    for i in range(3):
        await record(session, AuditKind.GITHUB_WRITE, AuditOutcome.ALLOWED, f"write {i}")
    events = await list_events(session, limit=2, offset=0)
    assert [e.summary for e in events] == ["write 2", "write 1"]
    assert [e.summary for e in await list_events(session, limit=2, offset=2)] == ["write 0"]
    assert events[0].at.tzinfo is not None


async def test_filter_by_kind(session: AsyncSession) -> None:
    await record(session, AuditKind.GITHUB_WRITE, AuditOutcome.BLOCKED, "blocked")
    await record(session, AuditKind.LOGIN, AuditOutcome.INFO, "login")
    events = await list_events(session, limit=10, offset=0, kind=AuditKind.LOGIN)
    assert [(e.kind, e.outcome) for e in events] == [(AuditKind.LOGIN, AuditOutcome.INFO)]


async def test_payload_and_run_id_are_stored(session: AsyncSession) -> None:
    run = await create_run(session, "manual", dry_run=True)
    await record(
        session,
        AuditKind.GITHUB_WRITE,
        AuditOutcome.DRY_RUN,
        "add label",
        {"issue": 4, "labels": ["skeleton HITL"]},
        run_id=run.id,
    )
    await session.commit()
    session.expunge_all()
    (event,) = await list_events(session, limit=1, offset=0)
    assert event.payload == {"issue": 4, "labels": ["skeleton HITL"]}
    assert event.run_id == run.id


async def test_long_summary_is_capped(session: AsyncSession) -> None:
    await record(session, AuditKind.POLICY_VIOLATION, AuditOutcome.BLOCKED, "x" * 2000)
    (event,) = await list_events(session, limit=1, offset=0)
    assert len(event.summary) == AUDIT_SUMMARY_LENGTH
