import pytest

from skeleton_crew.domain.job_policy import CiResult
from skeleton_crew.domain.schemas import (
    MissingDocUpdate,
    RefineResult,
    ReviewFinding,
    ReviewResult,
    SelectionDuplicate,
    SelectionPick,
    SelectionQuestion,
    SelectionResult,
)
from skeleton_crew.domain.task_state import TaskState
from skeleton_crew.domain.text_policy import TRUNCATION_SUFFIX
from skeleton_crew.pipeline.texts import (
    CI_ESCALATIONS,
    CONFLICTS_MESSAGE,
    UNREADABLE_PATCH,
    WORKER_NOT_RESUMABLE,
    branch_taken_over,
    merge_breaks_policy,
    pr_body,
    refine_comment,
    review_check_summary,
    review_text,
    review_too_large,
    selection_escalations,
    session_failed,
    setup_failed,
    worker_gave_up,
    worker_timeout_reason,
)


def _result(*docs: MissingDocUpdate, blockers: tuple[str, ...] = ()) -> ReviewResult:
    return ReviewResult(
        verdict="changes_requested",
        summary="Needs docs.",
        findings=[ReviewFinding(severity="major", path="app.py", line=3, message="No test.")],
        missing_doc_updates=list(docs),
        blockers=list(blockers),
    )


def test_review_text_lists_missing_doc_updates_after_findings() -> None:
    text = review_text(_result(MissingDocUpdate(path="README.md", reason="Old flag.")))
    assert text.splitlines() == [
        "Needs docs.",
        "- [major] app.py:3: No test.",
        "- [docs] README.md: Old flag.",
    ]


def test_review_check_summary_sanitises_missing_doc_updates() -> None:
    doc = MissingDocUpdate(path="docs/@octocat.md", reason="Fixes #12 \u202eevil")
    summary = review_check_summary(_result(doc), [], 1000)
    assert "@octocat" not in summary
    assert "Fixes #12" not in summary
    assert "\u202e" not in summary


def test_review_check_summary_caps_many_missing_doc_updates() -> None:
    docs = [MissingDocUpdate(path=f"docs/{n}.md", reason="x" * 500) for n in range(20)]
    summary = review_check_summary(_result(*docs), [], 1000)
    assert len(summary) == 1000
    assert summary.endswith(TRUNCATION_SUFFIX)


def test_review_check_summary_lists_round_blockers_by_role() -> None:
    summary = review_check_summary(_result(blockers=("Tests could not run",)), ["No Node"], 1000)
    assert summary.endswith(
        "- [major] app.py:3: No test.\n\n### Blockers\n\n"
        "- worker: No Node\n- reviewer: Tests could not run"
    )


def test_review_check_summary_omits_empty_blockers() -> None:
    assert "Blockers" not in review_check_summary(_result(), [], 1000)


def test_review_check_summary_sanitises_blockers() -> None:
    summary = review_check_summary(_result(blockers=("Closes #3",)), ["ask @octocat"], 1000)
    assert "@octocat" not in summary
    assert "Closes #3" not in summary


def test_pr_body_lists_blockers_after_the_summary() -> None:
    body = pr_body(1, "Greets.", ["No Python 3.13", "pypi.org blocked"], 1000)
    assert body == ("Closes #1\n\nGreets.\n\n### Blockers\n\n- No Python 3.13\n- pypi.org blocked")


def test_pr_body_omits_empty_blockers() -> None:
    assert pr_body(1, "Greets.", [], 1000) == "Closes #1\n\nGreets."


def test_pr_body_sanitises_blockers() -> None:
    body = pr_body(1, "Greets.", ["ping @octocat", "fixes #2 \u202eevil"], 1000)
    assert body.startswith("Closes #1\n\n")
    assert "@octocat" not in body
    assert "fixes #2" not in body
    assert "\u202e" not in body


def test_pr_body_caps_many_blockers_and_keeps_the_closing_line() -> None:
    body = pr_body(1, "Greets.", [f"{n} " + "x" * 290 for n in range(10)], 1000)
    assert len(body) == 1000
    assert body.startswith("Closes #1\n\n")
    assert body.endswith(TRUNCATION_SUFFIX)


def test_refine_comment_keeps_questions_when_the_spec_is_too_long() -> None:
    result = RefineResult(spec_markdown="s" * 3000, open_questions=["Which slot?", "Who approves?"])
    comment = refine_comment(result, 1500)
    assert len(comment) <= 1500
    assert comment.startswith("## Refined spec\n\nsss")
    assert TRUNCATION_SUFFIX in comment
    assert comment.endswith("### Open questions\n\n- Which slot?\n- Who approves?")


def test_refine_comment_caps_many_long_questions_to_half() -> None:
    result = RefineResult(spec_markdown="Goal.", open_questions=["q" * 300] * 10)
    comment = refine_comment(result, 1500)
    assert len(comment) <= 1500
    assert comment.startswith("## Refined spec\n\nGoal.\n\n### Open questions")
    assert comment.endswith(TRUNCATION_SUFFIX)


def test_refine_comment_without_questions_is_only_the_spec() -> None:
    comment = refine_comment(RefineResult(spec_markdown="Goal: greet."), 1500)
    assert comment == "## Refined spec\n\nGoal: greet."


def test_refine_comment_sanitises_questions() -> None:
    result = RefineResult(spec_markdown="Goal.", open_questions=["Ask @owner?"])
    assert "@owner" not in refine_comment(result, 1500)


def test_escalation_reasons_keep_their_exact_text() -> None:
    assert WORKER_NOT_RESUMABLE == "the worker session cannot be resumed"
    assert UNREADABLE_PATCH == "could not read the worker's changes from its workspace"
    assert CONFLICTS_MESSAGE == "Unresolved merge (local only)"
    assert CI_ESCALATIONS == {
        CiResult.NOT_REPORTED: "no CI was reported for the change",
        CiResult.TIMED_OUT: "CI did not finish in time",
    }
    assert setup_failed("exit 2") == "the setup command failed (exit 2)"
    assert session_failed("review", "boom") == "review session failed: boom"
    assert session_failed("worker", None) == "worker session failed: None"
    assert worker_gave_up("blocked", "No toolchain.") == "worker reported blocked: No toolchain."
    assert worker_gave_up("scope_too_large", None) == "worker reported scope_too_large: None"
    assert review_too_large("Huge.") == "the reviewer judged the change too large: Huge."
    assert branch_taken_over("skeleton/1-a", "PR #3 was closed without merging") == (
        "`skeleton/1-a`: PR #3 was closed without merging"
    )
    assert merge_breaks_policy(["a: protected path", "b: symlink"]) == (
        "merging the base branch breaks policy: a: protected path; b: symlink"
    )


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (
            TaskState.AWAITING_CI,
            "the worker timed out or the run window ended while waiting for CI",
        ),
        (TaskState.CODING, "the worker timed out or the run window ended"),
        (TaskState.REVIEWING, "the worker timed out or the run window ended"),
    ],
)
def test_the_timeout_says_when_it_hit_the_ci_wait(state: TaskState, reason: str) -> None:
    assert worker_timeout_reason(state) == reason


def test_selection_escalations_keep_their_text_and_order() -> None:
    selection = SelectionResult(
        picks=[SelectionPick(issue_number=9, reason="small")],
        too_large=[SelectionPick(issue_number=2, reason="huge")],
        needs_decision=[
            SelectionQuestion(
                issue_number=4, problem="Unclear.", question="Which?", options=["a", "b"]
            )
        ],
        rejected=[SelectionPick(issue_number=3, reason="unsafe")],
        duplicates=[SelectionDuplicate(issue_number=5, duplicate_of=1, reason="same change")],
    )
    assert selection_escalations(selection) == [
        (2, "the selector judged the issue too large: huge"),
        (4, "the selector needs a decision: Unclear.\n\nQuestion: Which?\n\nOptions:\n- a\n- b"),
        (3, "the selector rejected the issue: unsafe"),
        (5, "the selector found a duplicate of #1: same change"),
    ]


def test_a_plain_selection_escalates_nothing() -> None:
    assert selection_escalations(SelectionResult()) == []
