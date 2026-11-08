import csv
import io

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from skeleton_crew.store import runs_store
from skeleton_crew.web.rendering import render
from skeleton_crew.web.state import get_state

router = APIRouter()

PAGE_SIZE = 25

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: str) -> str:
    # Spreadsheets execute cells starting with these as formulas; the text is untrusted.
    return f"'{value}" if value.startswith(_FORMULA_PREFIXES) else value


@router.get("/blockers")
async def blockers(request: Request, page: int = Query(1, ge=1, le=100_000)) -> Response:
    async with get_state(request).sessionmaker() as session:
        tasks = await runs_store.list_tasks_with_blockers(
            session, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE
        )
    context = {
        "tasks": tasks[:PAGE_SIZE],
        "repo": get_state(request).config.repo,
        "page": page,
        "has_next": len(tasks) > PAGE_SIZE,
    }
    return render(request, "blockers.html", context)


@router.get("/blockers/export")
async def export(request: Request) -> Response:
    async with get_state(request).sessionmaker() as session:
        tasks = await runs_store.list_tasks_with_blockers(session)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["run", "task", "issue", "title", "kind", "state", "pr", "updated_at", "blocker"]
    )
    for task in tasks:
        for blocker in task.blockers or []:
            writer.writerow(
                [
                    task.run_id,
                    task.id,
                    task.issue_number,
                    _cell(task.issue_title),
                    task.kind.value,
                    task.state.value,
                    task.pr_number or "",
                    task.updated_at.isoformat(),
                    _cell(blocker),
                ]
            )
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="blockers.csv"'},
    )
