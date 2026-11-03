from fastapi import APIRouter, Request
from fastapi.responses import Response

from skeleton_crew.domain.settings import Limits
from skeleton_crew.web.forms import Kind, SettingsPage, number_specs

router = APIRouter()

_SPECS = number_specs("limits", Limits, Limits.model_fields)

_PAGE = SettingsPage(
    template="limits.html",
    fields={spec.name: Kind.OPTIONAL_NUMBER if spec.optional else Kind.NUMBER for spec in _SPECS},
    context={"specs": _SPECS},
)


@router.get("/limits")
async def show(request: Request) -> Response:
    return await _PAGE.show(request)


@router.post("/limits")
async def submit(request: Request) -> Response:
    return await _PAGE.submit(request)
