import pytest
from pydantic import ValidationError

from skeleton_crew.domain.runner_policy import (
    MAX_RESTART_DELAY_S,
    PUBLIC_REPO_PROBLEM,
    eligibility_problem,
    is_crash,
    restart_delay,
    runner_names,
)
from skeleton_crew.domain.settings import AppSettings, SelfHostedCi


def test_private_repo_is_eligible() -> None:
    assert eligibility_problem(private=True) is None


def test_public_repo_is_refused() -> None:
    assert eligibility_problem(private=False) == PUBLIC_REPO_PROBLEM


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
