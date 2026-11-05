from skeleton_crew.domain.schemas import MissingDocUpdate, ReviewFinding, ReviewResult
from skeleton_crew.domain.text_policy import TRUNCATION_SUFFIX
from skeleton_crew.pipeline.texts import pr_body, review_check_summary, review_text


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
