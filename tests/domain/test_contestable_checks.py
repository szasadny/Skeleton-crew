from skeleton_crew.domain.contest_policy import CheckId, CheckInput
from skeleton_crew.domain.contestable_checks import CHECKS, describe_checks, run_checks
from skeleton_crew.domain.diff_lines import AddedLine
from skeleton_crew.domain.settings import AppSettings

SETTINGS = AppSettings()
COMMENTED = [AddedLine("app.py", n, f"# line {n}") for n in range(1, 6)]


def test_every_check_id_has_exactly_one_registered_check() -> None:
    ids = [check.id for check in CHECKS]
    assert sorted(ids) == sorted(CheckId)


def test_each_check_only_emits_its_own_id() -> None:
    diff = CheckInput([], COMMENTED, SETTINGS)
    for check in CHECKS:
        assert {f.check for f in check.run(diff)} <= {check.id}


def test_run_checks_collects_every_finding() -> None:
    [finding] = run_checks(CheckInput([], COMMENTED, SETTINGS))
    assert (finding.check, finding.path, finding.line) == (CheckId.COMMENT_BLOCK, "app.py", 1)


def test_every_check_is_described_for_the_worker() -> None:
    described = describe_checks(SETTINGS)
    assert [check_id for check_id, _ in described] == [check.id for check in CHECKS]
    assert all(text.strip() for _, text in described)
