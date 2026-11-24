import pytest

from skeleton_crew.domain.contest_policy import (
    CHECKS,
    CheckId,
    CheckInput,
    Contest,
    Finding,
    ObjectionKey,
    comment_block_findings,
    contest,
    describe_checks,
    describe_comment_blocks,
    finding_violation,
    objection_key,
    run_checks,
    uncontested,
)
from skeleton_crew.domain.diff_parse import AddedLine
from skeleton_crew.domain.schemas import Objection
from skeleton_crew.domain.settings import AppSettings, Limits

BLOCK = Finding(CheckId.COMMENT_BLOCK, "app.py", 3, "comment block of 4 lines, limit is 2")
OTHER = Finding(CheckId.COMMENT_BLOCK, "lib/x.py", 10, "comment block of 5 lines, limit is 2")


def test_contested_finding_carries_the_reason() -> None:
    open_findings, contested = contest(
        [BLOCK, OTHER], {(CheckId.COMMENT_BLOCK, "app.py", 3): "License header."}
    )
    assert open_findings == [OTHER]
    assert contested == [Contest(BLOCK, "License header.")]


@pytest.mark.parametrize(
    "key",
    [
        (CheckId.COMMENT_BLOCK, "app.py", 4),
        (CheckId.COMMENT_BLOCK, "APP.py", 3),
        (CheckId.COMMENT_BLOCK, "other.py", 3),
        (CheckId.COMMENT_BLOCK, "lib/x.py/../app.py", 3),
        ("protected_path", "app.py", 3),
    ],
)
def test_objection_must_name_the_exact_finding(key: ObjectionKey) -> None:
    assert contest([BLOCK], {key: "Needed."}) == ([BLOCK], [])


def test_unused_objections_are_ignored() -> None:
    assert contest([], {(CheckId.COMMENT_BLOCK, "app.py", 3): "Needed."}) == ([], [])


def test_uncontested_findings_have_no_reason() -> None:
    assert uncontested([BLOCK]) == [Contest(BLOCK, None)]


def test_violation_text_names_the_check_to_contest() -> None:
    assert str(finding_violation(BLOCK)) == (
        "app.py:3: comment block of 4 lines, limit is 2 (contestable as `comment_block`)"
    )


def test_objection_key_matches_findings_and_agent_objections() -> None:
    objection = Objection(check=CheckId.COMMENT_BLOCK, path="app.py", line=3, reason="License.")
    assert objection_key(objection) == objection_key(BLOCK) == (CheckId.COMMENT_BLOCK, "app.py", 3)


SETTINGS = AppSettings()
COMMENTED = [AddedLine("app.py", n, f"# line {n}") for n in range(1, 6)]


def test_every_check_id_has_exactly_one_registered_check() -> None:
    ids = [check.id for check in CHECKS]
    assert sorted(ids) == sorted(CheckId)


def test_each_check_only_emits_its_own_id() -> None:
    diff = CheckInput(COMMENTED, SETTINGS)
    for check in CHECKS:
        assert {f.check for f in check.run(diff)} <= {check.id}


def test_run_checks_collects_every_finding() -> None:
    [finding] = run_checks(CheckInput(COMMENTED, SETTINGS))
    assert (finding.check, finding.path, finding.line) == (CheckId.COMMENT_BLOCK, "app.py", 1)


def test_every_check_is_described_for_the_worker() -> None:
    described = describe_checks(SETTINGS)
    assert [check_id for check_id, _ in described] == [check.id for check in CHECKS]
    assert all(text.strip() for _, text in described)


def test_comment_block_findings_use_the_configured_limit() -> None:
    settings = AppSettings(limits=Limits(max_code_comment_lines=2))
    added = [
        AddedLine("app.py", n, text)
        for n, text in enumerate(["# one", "# two", "# three", "code()"], 1)
    ]
    assert comment_block_findings(CheckInput(added, settings)) == [
        Finding(CheckId.COMMENT_BLOCK, "app.py", 1, "comment block of 3 lines, limit is 2")
    ]
    assert describe_comment_blocks(settings) == "comment blocks longer than 2 lines"
