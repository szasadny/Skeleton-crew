from fastapi import APIRouter, Request
from fastapi.responses import Response

from skeleton_crew.agents.registry import available_backends, field_problems
from skeleton_crew.classifiers import registry as classifiers
from skeleton_crew.domain.settings import AppSettings, Role
from skeleton_crew.web.forms import GENERAL, Kind, SettingsPage

router = APIRouter()

_ROLE_FIELDS = {
    "backend": Kind.TEXT,
    "model": Kind.TEXT,
    "effort": Kind.TEXT,
    "web_tools": Kind.BOOL,
}
_CLASSIFIER_FIELDS = {
    "provider": Kind.TEXT,
    "model": Kind.TEXT,
    "injection_screen": Kind.BOOL,
    "prescreen": Kind.BOOL,
    "effort_routing": Kind.BOOL,
    "effort_small": Kind.TEXT,
    "effort_medium": Kind.TEXT,
    "effort_large": Kind.TEXT,
}
_BACKENDS = available_backends()
_CLASSIFIERS = classifiers.available_classifiers()
_FIELDS = {
    **{f"agents.{role}.{name}": kind for role in Role for name, kind in _ROLE_FIELDS.items()},
    **{
        f"backend_options.{spec.name}.{option}": Kind.TEXT
        for spec in _BACKENDS
        for option in spec.options
    },
    **{f"classifier.{name}": kind for name, kind in _CLASSIFIER_FIELDS.items()},
}


def _problems(settings: AppSettings) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    found = field_problems(settings) | classifiers.field_problems(settings)
    for name, messages in found.items():
        errors.setdefault(name if name in _FIELDS else GENERAL, []).extend(messages)
    return errors


_PAGE = SettingsPage(
    template="agents.html",
    fields=_FIELDS,
    context={
        "roles": tuple(Role),
        "backends": _BACKENDS,
        "specs": {spec.name: spec for spec in _BACKENDS},
        "classifiers": {spec.name: spec for spec in _CLASSIFIERS},
    },
    check=_problems,
)


@router.get("/agents")
async def show(request: Request) -> Response:
    return await _PAGE.show(request)


@router.post("/agents")
async def submit(request: Request) -> Response:
    return await _PAGE.submit(request)
