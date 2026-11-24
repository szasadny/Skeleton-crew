import pytest
from pydantic import ValidationError

from skeleton_crew.domain.runner_policy import (
    CI_OFF_REASON,
    CRASH_REASON,
    DRAINING_REASON,
    KILL_SWITCH_REASON,
    MAX_RESTART_DELAY_S,
    NO_RUNNER_REGISTERED,
    PUBLIC_REPO_PROBLEM,
    STANDBY_REASON,
    STOPPED_REASON,
    RunnerSlot,
    RunnerState,
    RunnerStatus,
    after_exit,
    after_start_failure,
    eligibility_problem,
    is_crash,
    is_definite,
    restart_delay,
    retry_reason,
    runner_names,
    settings_blocker,
    standby_state,
    unreadable_visibility,
)
from skeleton_crew.domain.settings import AppSettings, SelfHostedCi


def test_private_repo_is_eligible() -> None:
    assert eligibility_problem(private=True) is None


def test_public_repo_is_refused() -> None:
    assert eligibility_problem(private=False) == PUBLIC_REPO_PROBLEM


def test_shown_texts_are_unchanged() -> None:
    assert PUBLIC_REPO_PROBLEM == (
        "self-hosted CI is only available for private repositories: "
        "jobs from fork pull requests would run on this host"
    )
    assert NO_RUNNER_REGISTERED == "self-hosted CI is on, but no runner is registered"
    assert CI_OFF_REASON == "self-hosted CI is off"
    assert KILL_SWITCH_REASON == "kill switch is engaged"
    assert STOPPED_REASON == "stopped"
    assert STANDBY_REASON == "no CI waits on a Skeleton Crew branch"
    assert DRAINING_REASON == "finishing its job"
    assert CRASH_REASON == "the runner stopped right after starting; check its registration"
    assert unreadable_visibility("TimeoutError") == (
        "cannot read the repository visibility: TimeoutError"
    )


def test_only_a_definite_visibility_answer_counts_as_definite() -> None:
    assert is_definite(None)
    assert is_definite(PUBLIC_REPO_PROBLEM)
    assert not is_definite(unreadable_visibility("RuntimeError"))


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (AppSettings(ci=SelfHostedCi(enabled=True)), None),
        (AppSettings(), CI_OFF_REASON),
        (AppSettings(kill_switch=True), CI_OFF_REASON),
        (AppSettings(ci=SelfHostedCi(enabled=True), kill_switch=True), KILL_SWITCH_REASON),
    ],
)
def test_ci_off_and_the_kill_switch_block_runners(
    settings: AppSettings, expected: str | None
) -> None:
    assert settings_blocker(settings) == expected


def test_standby_shows_why_only_for_registered_runners() -> None:
    assert standby_state("runner-1", registered=True) == RunnerState(
        "runner-1", RunnerStatus.STANDBY, STANDBY_REASON
    )
    assert standby_state("runner-2", registered=False) == RunnerState(
        "runner-2", RunnerStatus.NOT_REGISTERED
    )


def test_exit_right_after_start_counts_as_a_crash_and_backs_off() -> None:
    slot = after_exit(RunnerSlot(started_at=100.0, failures=1, reason="old"), 110.0)
    assert slot == RunnerSlot(
        started_at=100.0, failures=2, not_before=110.0 + restart_delay(2), reason=CRASH_REASON
    )
    assert retry_reason(slot, 115.0) == f"{CRASH_REASON} (retry in 15s)"


def test_exit_after_a_long_run_resets_the_backoff_and_keeps_the_reason() -> None:
    slot = after_exit(RunnerSlot(started_at=0.0, failures=3, reason="old"), 600.0)
    assert slot == RunnerSlot(started_at=0.0, failures=0, not_before=600.0, reason="old")
    assert retry_reason(slot, 600.0) is None


def test_start_failure_backs_off_with_the_error() -> None:
    slot = after_start_failure(RunnerSlot(started_at=50.0), 50.0, "no room")
    assert slot == RunnerSlot(started_at=50.0, failures=1, not_before=60.0, reason="no room")
    assert retry_reason(slot, 54.0) == "no room (retry in 6s)"
    assert retry_reason(slot, 60.0) is None


def test_runner_names_are_numbered_from_one() -> None:
    assert runner_names(3) == ["runner-1", "runner-2", "runner-3"]


def test_short_runs_count_as_crashes() -> None:
    assert is_crash(5.0)
    assert not is_crash(600.0)


def test_restart_delay_grows_and_is_capped() -> None:
    assert restart_delay(0) == 0.0
    assert restart_delay(1) < restart_delay(2) < restart_delay(3)
    assert restart_delay(1000) == MAX_RESTART_DELAY_S


def test_self_hosted_ci_is_off_by_default() -> None:
    assert AppSettings().ci.enabled is False


def test_default_runner_allowlist_has_no_storage_wildcard() -> None:
    allowlist = SelfHostedCi().egress_allowlist
    assert ".blob.core.windows.net" not in allowlist
    assert not any(domain.startswith(".") and "windows.net" in domain for domain in allowlist)
    assert "productionresultssa0.blob.core.windows.net" in allowlist


def test_runner_allowlist_is_separate_from_the_agent_allowlist() -> None:
    settings = AppSettings()
    assert "github.com" in settings.ci.egress_allowlist
    assert "github.com" not in settings.runtime.egress_allowlist


@pytest.mark.parametrize(
    "labels",
    [(), ("",), ("has space",), ("a,b",), ("-leading",), ("x" * 101,), tuple("abcdefghijk")],
)
def test_invalid_runner_labels_are_rejected(labels: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        SelfHostedCi(labels=labels)


@pytest.mark.parametrize("domain", ["*.blob.core.windows.net", "http://github.com", "localhost"])
def test_invalid_runner_egress_domains_are_rejected(domain: str) -> None:
    with pytest.raises(ValidationError):
        SelfHostedCi(egress_allowlist=(domain,))


@pytest.mark.parametrize("host", ["", "a b", "db,minio", "$(id)", "-x", "a" * 254])
def test_invalid_no_proxy_hosts_are_rejected(host: str) -> None:
    with pytest.raises(ValidationError):
        SelfHostedCi(no_proxy=(host,))


def test_no_proxy_accepts_names_ips_and_domains() -> None:
    hosts = ("db", "10.0.0.0/8", ".internal.example", "::1")
    assert SelfHostedCi(no_proxy=hosts).no_proxy == hosts


@pytest.mark.parametrize(("field", "value"), [("runners", 0), ("runners", 11), ("pids", 1)])
def test_out_of_range_runner_limits_are_rejected(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        SelfHostedCi.model_validate({field: value})
