from functools import partial
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from skeleton_crew.core.audit import AuditKind
from skeleton_crew.store import audit_store
from skeleton_crew.web.pagination import PageParam, paginate
from skeleton_crew.web.rendering import render
from skeleton_crew.web.state import SessionDep

router = APIRouter()

PAGE_SIZE = 50
_KIND_CHARS = 32


@router.get("/logs")
async def logs(
    request: Request,
    session: SessionDep,
    kind: Annotated[str, Query(max_length=_KIND_CHARS)] = "",
    page: PageParam = 1,
) -> Response:
    selected = AuditKind(kind) if kind in set(AuditKind) else None
    fetch = partial(audit_store.list_events, session, kind=selected)
    context = {
        "page": await paginate(fetch, page, PAGE_SIZE),
        "kinds": tuple(AuditKind),
        "kind": selected.value if selected else "",
    }
    return render(request, "logs.html", context)
