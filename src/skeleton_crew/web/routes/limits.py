from fastapi import APIRouter

from skeleton_crew.domain.settings import Limits
from skeleton_crew.web.forms import Kind, number_specs
from skeleton_crew.web.settings_page import SettingsPage

router = APIRouter()

_SPECS = number_specs("limits", Limits, Limits.model_fields)

SettingsPage(
    template="limits.html",
    fields={spec.name: Kind.OPTIONAL_NUMBER if spec.optional else Kind.NUMBER for spec in _SPECS},
    context={"specs": _SPECS},
).include(router, "/limits")
