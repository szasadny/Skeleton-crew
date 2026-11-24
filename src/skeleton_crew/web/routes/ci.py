from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter

from skeleton_crew.domain.branch_policy import BRANCH_PREFIX
from skeleton_crew.domain.settings import SelfHostedCi
from skeleton_crew.web.forms import Kind, number_specs
from skeleton_crew.web.settings_page import PageHooks, SettingsPage
from skeleton_crew.web.state import WebState

router = APIRouter()

_NUMBERS = number_specs("ci", SelfHostedCi, ["runners", "cpus", "memory_gb", "pids"])


def _live(state: WebState, _values: Mapping[str, object]) -> dict[str, Any]:
    return {
        "runners": state.runners.statuses(),
        "runners_dir": state.config.runners_dir,
        "runner_image": state.config.runner_image,
    }


SettingsPage(
    template="ci.html",
    fields={
        "ci.enabled": Kind.BOOL,
        "ci.labels": Kind.LINES,
        "ci.egress_allowlist": Kind.LINES,
        "ci.no_proxy": Kind.LINES,
        **{spec.name: Kind.NUMBER for spec in _NUMBERS},
    },
    context={"numbers": _NUMBERS, "branch_prefix": BRANCH_PREFIX},
    hooks=PageHooks(live_context=_live),
).include(router, "/ci")
