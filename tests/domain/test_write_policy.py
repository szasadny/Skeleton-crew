import pytest

from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.settings import LabelNames
from skeleton_crew.domain.write_policy import (
    AUDIT_TEXT_CHARS,
    MAX_TITLE_CHARS,
    check_new_issue,
    check_open_pr,
    check_push,
    check_sha,
    check_title,
    new_issue_labels,
    short,
)

SHA = "a" * 40


def test_short_caps_audit_text() -> None:
    assert short("x" * 500) == "x" * AUDIT_TEXT_CHARS
    assert short("abc") == "abc"


@pytest.mark.parametrize("title", ["Fix it", "x" * MAX_TITLE_CHARS, " padded "])
def test_single_line_titles_pass(title: str) -> None:
    check_title(title)


@pytest.mark.parametrize("title", ["", "   ", "x" * (MAX_TITLE_CHARS + 1), "a\nb", "a\rb"])
def test_bad_titles_are_rejected(title: str) -> None:
    with pytest.raises(PolicyViolation, match="title not allowed"):
        check_title(title)


def test_rejected_title_is_shown_capped() -> None:
    with pytest.raises(PolicyViolation) as raised:
        check_title("y" * 1000)
    assert str(raised.value) == f"title not allowed: {'y' * AUDIT_TEXT_CHARS!r}"


@pytest.mark.parametrize("sha", ["main", "a" * 39, "A" * 40, ""])
def test_non_shas_are_rejected(sha: str) -> None:
    with pytest.raises(PolicyViolation, match="not a full commit sha"):
        check_sha(sha)


def test_new_issue_only_ever_gets_the_hitl_label() -> None:
    labels = LabelNames(hitl="needs a human")
    assert new_issue_labels(labels) == ["needs a human"]
    check_new_issue("Follow-up", labels)
    with pytest.raises(PolicyViolation):
        check_new_issue("two\nlines", labels)


def test_pull_requests_only_come_from_skeleton_branches() -> None:
    check_open_pr("skeleton/4-fix", "Fix", "main")
    with pytest.raises(PolicyViolation, match="push target"):
        check_open_pr("main", "Fix", "main")
    with pytest.raises(PolicyViolation, match="title"):
        check_open_pr("skeleton/4-fix", "", "main")


def test_pushes_need_a_skeleton_branch_and_a_full_sha() -> None:
    check_push("skeleton/4-fix", SHA, "main")
    with pytest.raises(PolicyViolation, match="push target"):
        check_push("feature/x", SHA, "main")
    with pytest.raises(PolicyViolation, match="push target is the base branch"):
        check_push("skeleton/4-fix", SHA, "skeleton/4-fix")
    with pytest.raises(PolicyViolation, match="not a full commit sha"):
        check_push("skeleton/4-fix", "HEAD", "main")
