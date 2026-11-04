import pytest

from skeleton_crew.domain.branch_policy import (
    BRANCH_PREFIX,
    branch_name,
    check_push_target,
    continuation_problem,
    issue_number_from_branch,
    pushed_by_controller,
)
from skeleton_crew.domain.errors import PolicyViolation


@pytest.mark.parametrize(
    ("number", "title", "expected"),
    [
        (12, "Fix login bug", "skeleton/12-fix-login-bug"),
        (1, "  Leading and trailing  ", "skeleton/1-leading-and-trailing"),
        (5, "Café crème brûlée", "skeleton/5-cafe-creme-brulee"),
        (5, "a -- b __ c", "skeleton/5-a-b-c"),
        (5, "", "skeleton/5-issue"),
        (5, "!!!", "skeleton/5-issue"),
        (5, "日本語", "skeleton/5-issue"),
        (5, "../../etc/passwd", "skeleton/5-etc-passwd"),
        (5, "feat: add `x`; rm -rf /", "skeleton/5-feat-add-x-rm-rf"),
        (5, "UPPER Case", "skeleton/5-upper-case"),
        (5, "x\nmain", "skeleton/5-x-main"),
        (9_999_999_999, "t", "skeleton/9999999999-t"),
    ],
)
def test_branch_name(number: int, title: str, expected: str) -> None:
    assert branch_name(number, title) == expected


def test_slug_is_capped_at_forty_chars_without_trailing_hyphen() -> None:
    name = branch_name(3, "word " * 30)
    slug = name.removeprefix("skeleton/3-")
    assert len(slug) <= 40
    assert not slug.endswith("-")


@pytest.mark.parametrize("number", [0, -1, 10_000_000_000])
def test_branch_name_rejects_invalid_issue_numbers(number: int) -> None:
    with pytest.raises(PolicyViolation):
        branch_name(number, "x")


@pytest.mark.parametrize(
    "title", ["", "a" * 200, "Ä" * 50, "x-" * 30, "--", "🐛 bug", "\x00\x01", "a b" * 40]
)
def test_generated_names_pass_the_push_check(title: str) -> None:
    name = branch_name(42, title)
    check_push_target(name, "main")
    assert issue_number_from_branch(name) == 42
    assert name.startswith(BRANCH_PREFIX)


@pytest.mark.parametrize(
    ("branch", "number"),
    [("skeleton/12-x", 12), ("skeleton/1-a-b-c", 1), ("skeleton/9999999999-z", 9_999_999_999)],
)
def test_issue_number_from_branch(branch: str, number: int) -> None:
    assert issue_number_from_branch(branch) == number


BAD_BRANCHES = [
    "main",
    "master",
    "skeleton/",
    "skeleton/12",
    "skeleton/12-",
    "skeleton/-x",
    "skeleton/0-x",
    "skeleton/012-x",
    "skeleton/12-X",
    "skeleton/12-x-",
    "skeleton/12--x",
    "skeleton/12-x_y",
    "skeleton/12-x/y",
    "skeleton/12-x/../main",
    "skeleton/12-x\n",
    "skeleton/12-x ",
    " skeleton/12-x",
    "Skeleton/12-x",
    "refs/heads/skeleton/12-x",
    "skeleton/12345678901-x",
    "skeleton/12-" + "a" * 41,
    "skeleton/12-x.lock",
    "skeleton/12-x:main",
    "skeleton/\uff112-x",
    "skeleton/12-é",
    "other/12-x",
    "+skeleton/12-x",
    "skeleton/12-x\x00",
]


@pytest.mark.parametrize("branch", BAD_BRANCHES)
def test_push_to_non_skeleton_branch_is_rejected(branch: str) -> None:
    with pytest.raises(PolicyViolation):
        check_push_target(branch, "main")


@pytest.mark.parametrize("branch", BAD_BRANCHES)
def test_issue_number_from_invalid_branch_is_none(branch: str) -> None:
    assert issue_number_from_branch(branch) is None


def test_push_to_base_branch_is_rejected_even_if_it_looks_like_a_skeleton_branch() -> None:
    with pytest.raises(PolicyViolation, match="base"):
        check_push_target("skeleton/1-x", "skeleton/1-x")


def test_slug_of_exactly_forty_chars_is_allowed() -> None:
    check_push_target("skeleton/1-" + "a" * 40, "main")


OURS = "a" * 40


@pytest.mark.parametrize(
    ("head", "last_pushed", "owned"),
    [
        (OURS, OURS, True),
        (OURS, None, False),
        (OURS, "b" * 40, False),
        ("", None, False),
        (OURS.upper(), OURS, False),
    ],
)
def test_pushed_by_controller(head: str, last_pushed: str | None, owned: bool) -> None:
    assert pushed_by_controller(head, last_pushed) is owned


def test_owned_branch_without_closed_pr_may_be_continued() -> None:
    assert continuation_problem(OURS, OURS, None) is None


def test_foreign_head_is_a_problem_even_without_a_closed_pr() -> None:
    assert continuation_problem(OURS, "b" * 40, None) == (
        "the branch has commits Skeleton Crew did not push"
    )


def test_ownership_is_checked_before_the_closed_pr() -> None:
    problem = continuation_problem(OURS, None, 7)
    assert problem == "the branch has commits Skeleton Crew did not push"


def test_closed_pr_is_never_resurrected() -> None:
    assert continuation_problem(OURS, OURS, 7) == "PR #7 was closed without merging"
