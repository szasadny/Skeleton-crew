import pytest

from skeleton_crew.domain.contest_policy import (
    CheckId,
    Contest,
    Finding,
    ObjectionKey,
    contest,
    uncontested,
    violation_text,
)

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
    assert violation_text(BLOCK) == (
        "app.py:3: comment block of 4 lines, limit is 2 (contestable as `comment_block`)"
    )
