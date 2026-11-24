from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter

from skeleton_crew.agents.registry import field_problems
from skeleton_crew.classifiers import registry as classifiers
from skeleton_crew.domain.settings import CLASSIFIER_USE_CASES, ISSUE_SIZES, AppSettings, Role
from skeleton_crew.web.forms import Kind
from skeleton_crew.web.settings_page import PageHooks, SettingsPage
from skeleton_crew.web.state import WebState
from skeleton_crew.web.views.agents import BACKENDS, CLASSIFIERS, agents_context

router = APIRouter()

_ROLE_FIELDS = {
    "backend": Kind.TEXT,
    "model": Kind.TEXT,
    "effort": Kind.TEXT,
    "web_tools": Kind.BOOL,
}
_FIELDS = {
    **{f"agents.{role}.{name}": kind for role in Role for name, kind in _ROLE_FIELDS.items()},
    **{
        f"backend_options.{spec.name}.{option}": Kind.TEXT
        for spec in BACKENDS.values()
        for option in spec.options
    },
    "classifier.provider": Kind.TEXT,
    "classifier.model": Kind.TEXT,
    **{f"classifier.{name}": Kind.BOOL for name in CLASSIFIER_USE_CASES},
    **{f"classifier.effort_{size}": Kind.TEXT for size in ISSUE_SIZES},
}


def _problems(settings: AppSettings) -> dict[str, list[str]]:
    return field_problems(settings) | classifiers.field_problems(settings)


def _live(state: WebState, values: Mapping[str, object]) -> dict[str, Any]:
    return agents_context(values, state.config.agent_credentials)


SettingsPage(
    template="agents.html",
    fields=_FIELDS,
    context={
        "backend_names": tuple(BACKENDS),
        "classifier_names": tuple(CLASSIFIERS),
        "issue_sizes": ISSUE_SIZES,
    },
    hooks=PageHooks(check=_problems, live_context=_live),
).include(router, "/agents", draft=True)
