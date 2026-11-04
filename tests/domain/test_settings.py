from datetime import time
from typing import Any

import pytest
from pydantic import ValidationError

from skeleton_crew.domain.settings import (
    AppSettings,
    LabelNames,
    Limits,
    RepoPolicy,
    Role,
    RoleAgent,
    RuntimeLimits,
    Schedule,
)


def test_defaults_are_safe() -> None:
    settings = AppSettings()
    assert settings.dry_run is True
    assert settings.kill_switch is False
    assert ".github/**" in settings.repo.protected_paths
    assert set(settings.agents) == set(Role)
    assert settings.labels == LabelNames(
        ready="skeleton", refine="skeleton refine", hitl="skeleton HITL"
    )


def test_settings_round_trip_through_json() -> None:
    settings = AppSettings(schedule=Schedule(start_time=time(3, 30)), dry_run=False)
    assert AppSettings.model_validate_json(settings.model_dump_json()) == settings


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AppSettings.model_validate({"allow_merge": True})


def test_settings_are_frozen() -> None:
    settings = AppSettings()
    with pytest.raises(ValidationError):
        settings.dry_run = False


@pytest.mark.parametrize(
    "labels",
    [
        {"ready": "x", "refine": "x", "hitl": "y"},
        {"ready": "x", "refine": "X", "hitl": "y"},
        {"ready": "", "refine": "a", "hitl": "b"},
        {"ready": "   ", "refine": "a", "hitl": "b"},
        {"ready": "a" * 51, "refine": "b", "hitl": "c"},
    ],
)
def test_invalid_label_names_are_rejected(labels: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        LabelNames(**labels)


@pytest.mark.parametrize("pattern", ["", "/abs/**", "../x", "a/../b"])
def test_invalid_protected_patterns_are_rejected(pattern: str) -> None:
    with pytest.raises(ValidationError):
        RepoPolicy(protected_paths=(pattern,))


@pytest.mark.parametrize("login", ["-bad", "has space", "a" * 40, "x[bot]x", "", "a_b"])
def test_invalid_trusted_logins_are_rejected(login: str) -> None:
    with pytest.raises(ValidationError):
        RepoPolicy(trusted_users=(login,))


@pytest.mark.parametrize("login", ["octocat", "a-b", "skeleton-crew[bot]", "a" * 39])
def test_valid_trusted_logins_are_accepted(login: str) -> None:
    assert RepoPolicy(trusted_users=(login,)).trusted_users == (login,)


@pytest.mark.parametrize(
    "domain",
    ["", "localhost", "-a.com", "a..com", "evil.com/path", "*.evil.com", "A.COM", "1.2.3.4"],
)
def test_invalid_egress_domains_are_rejected(domain: str) -> None:
    with pytest.raises(ValidationError):
        RuntimeLimits(egress_allowlist=(domain,))


@pytest.mark.parametrize("domain", ["pypi.org", ".github.com", "a-b.example.co.uk"])
def test_valid_egress_domains_are_accepted(domain: str) -> None:
    assert RuntimeLimits(egress_allowlist=(domain,)).egress_allowlist == (domain,)


@pytest.mark.parametrize("model", ["opus", "claude-opus-4-1", "sonnet[1m]", "a/b:c"])
def test_valid_model_names(model: str) -> None:
    assert RoleAgent(model=model).model == model


@pytest.mark.parametrize("model", ["", "op us", "x;rm -rf", "a" * 101, "--dangerous flag"])
def test_invalid_model_names_are_rejected(model: str) -> None:
    with pytest.raises(ValidationError):
        RoleAgent(model=model)


@pytest.mark.parametrize("tz", ["Mars/Base", "", "../etc/passwd"])
def test_invalid_timezone_is_rejected(tz: str) -> None:
    with pytest.raises(ValidationError):
        Schedule(timezone=tz)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issues_per_day", -1),
        ("issues_per_day", 51),
        ("max_parallel_workers", 0),
        ("max_diff_lines", 9),
        ("max_comment_chars", 199),
        ("max_code_comment_lines", 0),
        ("max_budget_usd_per_session", 0),
        ("command_timeout_minutes", 0),
        ("command_timeout_minutes", 241),
    ],
)
def test_out_of_range_limits_are_rejected(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        Limits.model_validate({field: value})


@pytest.mark.parametrize("field", ["setup_command", "check_command"])
def test_repo_commands_are_optional_and_bounded(field: str) -> None:
    assert getattr(RepoPolicy(), field) is None
    with pytest.raises(ValidationError):
        RepoPolicy.model_validate({field: "x" * 501})


def test_missing_role_agent_is_rejected() -> None:
    with pytest.raises(ValidationError, match="missing agent config"):
        AppSettings(agents={Role.WORKER: RoleAgent()})
