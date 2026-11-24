from pathlib import Path

from migration_helpers import OLD_AT, downgrade, old_engine, seed_old_run, upgrade
from sqlalchemy import Connection, text

from skeleton_crew.core.db import make_sessionmaker
from skeleton_crew.domain.task_state import TaskKind
from skeleton_crew.store import runs_store

_REVISION = "0006"
_KINDS = {1: "issue", 2: "pr_update", 3: "pr_fix"}
_INSERT_TASK = text(
    "INSERT INTO worker_tasks (id, run_id, issue_number, issue_title, state, kind,"
    " check_rounds, ci_rounds, review_rounds, policy_rounds, cost_usd, note,"
    " blockers, created_at, updated_at)"
    " VALUES (:id, 1, :id, 'Fix it', 'done', :kind, 0, 0, 0, 0, 0.0, NULL, NULL, :at, :at)"
)


def _stored_kinds(connection: Connection) -> dict[int, str]:
    rows = connection.execute(text("SELECT id, kind FROM worker_tasks ORDER BY id")).all()
    return {row[0]: row[1] for row in rows}


async def test_pr_update_rows_become_pr_fix_and_stay_so_on_downgrade(tmp_path: Path) -> None:
    async with old_engine(tmp_path, _REVISION) as engine:
        async with engine.begin() as connection:
            await seed_old_run(connection)
            for task_id, kind in _KINDS.items():
                await connection.execute(_INSERT_TASK, {"id": task_id, "kind": kind, "at": OLD_AT})
        async with engine.begin() as connection:
            await connection.run_sync(upgrade, "head")
        async with make_sessionmaker(engine)() as session:
            tasks = [await runs_store.get_worker_task(session, task_id) for task_id in _KINDS]
        assert [task.kind if task else None for task in tasks] == [
            TaskKind.ISSUE,
            TaskKind.PR_FIX,
            TaskKind.PR_FIX,
        ]
        async with engine.begin() as connection:
            await connection.run_sync(downgrade, _REVISION)
            assert await connection.run_sync(_stored_kinds) == {
                1: "issue",
                2: "pr_fix",
                3: "pr_fix",
            }
