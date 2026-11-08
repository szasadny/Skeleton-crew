from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse, Response

from skeleton_crew.core.audit import AuditKind
from skeleton_crew.store import audit_store
from skeleton_crew.web.rendering import render
from skeleton_crew.web.state import get_state

router = APIRouter()

PAGE_SIZE = 50


@router.get("/logs")
async def logs(
    request: Request, kind: str = Query("", max_length=32), page: int = Query(1, ge=1, le=100_000)
) -> Response:
    selected = AuditKind(kind) if kind in set(AuditKind) else None
    async with get_state(request).sessionmaker() as session:
        events = await audit_store.list_events(
            session, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE, selected
        )
    context = {
        "events": events[:PAGE_SIZE],
        "page": page,
        "has_next": len(events) > PAGE_SIZE,
        "kinds": tuple(AuditKind),
        "kind": selected.value if selected else "",
    }
    return render(request, "logs.html", context)


@router.get("/audit")
async def audit_redirect(request: Request) -> RedirectResponse:
    query = request.url.query
    return RedirectResponse(f"/logs?{query}" if query else "/logs", status_code=308)
