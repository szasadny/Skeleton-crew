from pathlib import Path

from migration_helpers import OLD_AT, downgrade, old_engine, seed_old_run, upgrade
from sqlalchemy import Connection, text

from skeleton_crew.core.db import make_sessionmaker
from skeleton_crew.store import runs_store

_REVISION = "0003"


def _old_reason(connection: Connection) -> object:
    return connection.execute(text("SELECT escalation_reason FROM worker_tasks")).scalar_one()


async def test_escalation_reason_moves_into_note_and_back(tmp_path: Path) -> None:
    async with old_engine(tmp_path, _REVISION) as engine:
        async with engine.begin() as connection:
            await seed_old_run(connection)
            await connection.execute(
                text(
                    "INSERT INTO worker_tasks (id, run_id, issue_number, issue_title, state, kind,"
                    " ci_rounds, review_rounds, policy_rounds, cost_usd, escalation_reason,"
                    " created_at, updated_at)"
                    " VALUES (1, 1, 7, 'Fix it', 'escalated', 'issue', 0, 0, 0, 0.0,"
                    " 'CI still fails', :at, :at)"
                ),
                {"at": OLD_AT},
            )
        async with engine.begin() as connection:
            await connection.run_sync(upgrade, "head")
        async with make_sessionmaker(engine)() as session:
            task = await runs_store.get_worker_task(session, 1)
        assert task is not None
        assert task.note == "CI still fails"
        async with engine.begin() as connection:
            await connection.run_sync(downgrade, _REVISION)
            assert await connection.run_sync(_old_reason) == "CI still fails"
