from skeleton_crew.domain.schemas import MissingDocUpdate, ReviewFinding, ReviewResult
from skeleton_crew.domain.text_policy import TRUNCATION_SUFFIX
from skeleton_crew.pipeline.texts import review_check_summary, review_text


def _result(*docs: MissingDocUpdate) -> ReviewResult:
    return ReviewResult(
        verdict="changes_requested",
        summary="Needs docs.",
        findings=[ReviewFinding(severity="major", path="app.py", line=3, message="No test.")],
        missing_doc_updates=list(docs),
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
    summary = review_check_summary(_result(doc), 1000)
    assert "@octocat" not in summary
    assert "Fixes #12" not in summary
    assert "\u202e" not in summary


def test_review_check_summary_caps_many_missing_doc_updates() -> None:
    docs = [MissingDocUpdate(path=f"docs/{n}.md", reason="x" * 500) for n in range(20)]
    summary = review_check_summary(_result(*docs), 1000)
    assert len(summary) == 1000
    assert summary.endswith(TRUNCATION_SUFFIX)
