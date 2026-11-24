from functools import partial

from fastapi import APIRouter, Request
from fastapi.responses import Response

from skeleton_crew.store import runs_store
from skeleton_crew.web.exports import blockers_csv
from skeleton_crew.web.pagination import PageParam, paginate
from skeleton_crew.web.rendering import render
from skeleton_crew.web.state import SessionDep

router = APIRouter()


@router.get("/blockers")
async def blockers(request: Request, session: SessionDep, page: PageParam = 1) -> Response:
    tasks = await paginate(partial(runs_store.list_tasks_with_blockers, session), page)
    return render(request, "blockers.html", {"page": tasks})


@router.get("/blockers/export")
async def export(session: SessionDep) -> Response:
    tasks = await runs_store.list_tasks_with_blockers(session)
    return Response(
        blockers_csv(tasks),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="blockers.csv"'},
    )
