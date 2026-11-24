from fastapi import APIRouter

from skeleton_crew.domain.settings import RuntimeLimits
from skeleton_crew.web.forms import Kind, number_specs
from skeleton_crew.web.settings_page import SettingsPage

router = APIRouter()

_RESOURCES = number_specs("runtime", RuntimeLimits, ["cpus", "memory_gb", "pids"])

SettingsPage(
    template="repo.html",
    fields={
        "labels.ready": Kind.TEXT,
        "labels.refine": Kind.TEXT,
        "labels.hitl": Kind.TEXT,
        "repo.base_branch": Kind.TEXT,
        "repo.protected_paths": Kind.LINES,
        "repo.setup_command": Kind.OPTIONAL_TEXT,
        "repo.check_command": Kind.OPTIONAL_TEXT,
        "repo.require_ci": Kind.BOOL,
        "repo.allow_unprotected_base_branch": Kind.BOOL,
        "repo.trusted_users": Kind.LINES,
        "runtime.egress_allowlist": Kind.LINES,
        **{spec.name: Kind.NUMBER for spec in _RESOURCES},
    },
    context={"resources": _RESOURCES},
).include(router, "/repo")
