import csv
import io
from collections.abc import Callable, Iterable

from skeleton_crew.models.worker_task import WorkerTask

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

type _Getter = Callable[[WorkerTask, str], object]


def _cell(value: str) -> str:
    # Spreadsheets execute cells starting with these as formulas; the text is untrusted.
    return f"'{value}" if value.startswith(_FORMULA_PREFIXES) else value


_BLOCKER_COLUMNS: tuple[tuple[str, _Getter], ...] = (
    ("run", lambda task, _: task.run_id),
    ("task", lambda task, _: task.id),
    ("issue", lambda task, _: task.issue_number),
    ("title", lambda task, _: _cell(task.issue_title)),
    ("kind", lambda task, _: task.kind.value),
    ("state", lambda task, _: task.state.value),
    ("pr", lambda task, _: task.pr_number or ""),
    ("updated_at", lambda task, _: task.updated_at.isoformat()),
    ("blocker", lambda _, blocker: _cell(blocker)),
)


def blockers_csv(tasks: Iterable[WorkerTask]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(name for name, _ in _BLOCKER_COLUMNS)
    for task in tasks:
        for blocker in task.blockers or []:
            writer.writerow(get(task, blocker) for _, get in _BLOCKER_COLUMNS)
    return buffer.getvalue()
