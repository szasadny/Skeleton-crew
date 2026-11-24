from pydantic import SecretStr

from skeleton_crew.domain.settings import (
    CLASSIFIER_USE_CASES,
    ISSUE_SIZES,
    AppSettings,
    ClassifierSettings,
    Role,
    routed_effort_field,
)
from skeleton_crew.web.forms import form_values
from skeleton_crew.web.views.agents import BACKENDS, BackendView, ClassifierView, agents_context

_CLAUDE = BACKENDS["claude_code"]
_TOKEN = {"claude_oauth_token": SecretStr("oauth-token-value-for-tests")}


def test_use_cases_and_issue_sizes_cover_the_classifier_settings() -> None:
    efforts = {routed_effort_field(size) for size in ISSUE_SIZES}
    names = {"provider", "model", *CLASSIFIER_USE_CASES, *efforts}
    assert names == set(ClassifierSettings.model_fields)
    assert tuple(ClassifierSettings().routed_efforts) == ISSUE_SIZES


def test_backend_status_follows_use_and_credentials() -> None:
    assert BackendView(_CLAUDE, {}, (), ()).status == "unused"
    assert BackendView(_CLAUDE, {}, (Role.WORKER,), ("missing",)).status == "no credential"
    assert BackendView(_CLAUDE, {}, (Role.WORKER,), ()).status == "ready"


def test_classifier_status_follows_toggles_and_credentials() -> None:
    assert ClassifierView((), False, ("missing",)).status == "off"
    assert ClassifierView((), True, ("missing",)).status == "no credential"
    assert ClassifierView((), True, ()).status == "ready"


def test_backend_options_fall_back_to_the_spec_defaults() -> None:
    context = agents_context(form_values(AppSettings()), _TOKEN)
    claude = next(view for view in context["backends"] if view.spec is _CLAUDE)
    defaults = {name: option.default for name, option in _CLAUDE.options.items()}
    assert claude.options == defaults
    assert claude.status == "ready"


def test_an_empty_option_value_means_the_default() -> None:
    values = form_values(AppSettings()) | {"backend_options.claude_code.auth_mode": ""}
    claude = next(v for v in agents_context(values, _TOKEN)["backends"] if v.spec is _CLAUDE)
    assert claude.options["auth_mode"] == _CLAUDE.options["auth_mode"].default


def test_classifier_is_enabled_by_any_use_case() -> None:
    for name in CLASSIFIER_USE_CASES:
        values = form_values(AppSettings()) | {f"classifier.{name}": True}
        assert agents_context(values, {})["classifier"].enabled is True
    assert agents_context(form_values(AppSettings()), {})["classifier"].enabled is False
