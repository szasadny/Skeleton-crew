import csv
import io
from datetime import UTC, datetime

from skeleton_crew.domain.task_state import TaskKind, TaskState
from skeleton_crew.models.worker_task import WorkerTask
from skeleton_crew.web.exports import blockers_csv

_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _task(
    blockers: list[str] | None, title: str = "Add retries", pr: int | None = 42
) -> WorkerTask:
    return WorkerTask(
        id=3,
        run_id=1,
        issue_number=12,
        issue_title=title,
        kind=TaskKind.ISSUE,
        state=TaskState.ESCALATED,
        pr_number=pr,
        updated_at=_AT,
        blockers=blockers,
    )


def test_blockers_csv_writes_a_header_and_one_row_per_blocker() -> None:
    text = blockers_csv([_task(["No docker", "a, b"]), _task(None), _task([])])
    assert text == (
        "run,task,issue,title,kind,state,pr,updated_at,blocker\r\n"
        "1,3,12,Add retries,issue,escalated,42,2026-01-02T03:04:05+00:00,No docker\r\n"
        '1,3,12,Add retries,issue,escalated,42,2026-01-02T03:04:05+00:00,"a, b"\r\n'
    )


def test_blockers_csv_leaves_the_pr_empty_without_one() -> None:
    rows = list(csv.reader(io.StringIO(blockers_csv([_task(["x"], pr=None)]))))
    assert rows[1][6] == ""


def test_blockers_csv_defuses_spreadsheet_formulas_in_untrusted_text() -> None:
    formulas = ["=SUM(A1)", "+1", "-1", "@cmd", "\tx", "\rx"]
    rows = list(csv.reader(io.StringIO(blockers_csv([_task(formulas, title="=evil()")]))))
    assert [row[8] for row in rows[1:]] == [f"'{value}" for value in formulas]
    assert {row[3] for row in rows[1:]} == {"'=evil()"}
