from pathlib import Path

from migration_helpers import OLD_AT, columns, downgrade, old_engine, seed_old_run, upgrade
from sqlalchemy import text

from skeleton_crew.core.db import make_sessionmaker
from skeleton_crew.store import runs_store

_REVISION = "0004"


async def test_blockers_column_is_added_empty_and_dropped_again(tmp_path: Path) -> None:
    async with old_engine(tmp_path, _REVISION) as engine:
        async with engine.begin() as connection:
            await seed_old_run(connection)
            await connection.execute(
                text(
                    "INSERT INTO worker_tasks (id, run_id, issue_number, issue_title, state, kind,"
                    " ci_rounds, review_rounds, policy_rounds, cost_usd, note,"
                    " created_at, updated_at)"
                    " VALUES (1, 1, 7, 'Fix it', 'done', 'issue', 0, 0, 0, 0.0, NULL, :at, :at)"
                ),
                {"at": OLD_AT},
            )
        async with engine.begin() as connection:
            await connection.run_sync(upgrade, "head")
        sessionmaker = make_sessionmaker(engine)
        async with sessionmaker() as session:
            task = await runs_store.get_worker_task(session, 1)
            assert task is not None
            assert task.blockers is None
            await runs_store.update_worker_task(session, 1, blockers=["worker: no Python 3.13"])
            await session.commit()
        async with sessionmaker() as session:
            task = await runs_store.get_worker_task(session, 1)
        assert task is not None
        assert task.blockers == ["worker: no Python 3.13"]
        async with engine.begin() as connection:
            await connection.run_sync(downgrade, _REVISION)
            assert "blockers" not in await connection.run_sync(columns("worker_tasks"))
