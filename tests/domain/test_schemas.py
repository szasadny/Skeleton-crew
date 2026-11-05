import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from skeleton_crew.domain.contest_policy import CheckId
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.schemas import (
    MAX_OUTPUT_CHARS,
    MissingDocUpdate,
    NewIssue,
    RefineResult,
    ReviewFinding,
    ReviewResult,
    SelectionPick,
    SelectionResult,
    WorkerResult,
    check_selection,
    json_schema,
    parse_output,
)


def worker(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "status": "done",
        "summary": "Did it.",
        "pr_title": "Add x",
        "blockers": [],
    }
    data.update(overrides)
    return json.dumps(data)


def review(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "verdict": "approve",
        "summary": "Fine.",
        "findings": [],
        "missing_doc_updates": [],
        "blockers": [],
    }
    data.update(overrides)
    return json.dumps(data)


def finding(severity: str = "minor", **overrides: Any) -> dict[str, Any]:
    return {"severity": severity, "message": "m", **overrides}


def doc(**overrides: Any) -> dict[str, Any]:
    return {"path": "docs/usage.md", "reason": "Describes the old flag.", **overrides}


def test_minimal_worker_result_parses() -> None:
    result = parse_output(WorkerResult, worker())
    assert result == WorkerResult(status="done", summary="Did it.", pr_title="Add x", blockers=[])
    assert result.new_issues == []


def test_strings_are_stripped() -> None:
    result = parse_output(WorkerResult, worker(summary="  padded \n", pr_title=" t "))
    assert (result.summary, result.pr_title) == ("padded", "t")


@pytest.mark.parametrize(
    "raw",
    [
        worker(status="merged"),
        worker(status="DONE"),
        worker(status="blocked"),
        worker(status="scope_too_large"),
        worker(status="blocked", note="   "),
        worker(summary=""),
        worker(summary="   "),
        worker(summary="x" * 1501),
        worker(pr_title="x" * 73),
        worker(pr_title="two\nlines"),
        worker(pr_title="tab\there"),
        worker(note="x" * 1501),
        worker(extra="field"),
        worker(merge=True),
        worker(new_issues=[{"title": "t", "body": "b"}] * 11),
        worker(new_issues=[{"title": "t", "body": "b", "labels": ["skeleton"]}]),
        worker(new_issues=[{"title": "", "body": "b"}]),
        worker(new_issues=[{"title": "x" * 121, "body": "b"}]),
        worker(new_issues=[{"title": "a\nb", "body": "b"}]),
        worker(new_issues=[{"title": "t", "body": "x" * 4001}]),
        worker(new_issues="not a list"),
        worker(objections=[{"check": "comment_block", "path": "a.py", "line": 0, "reason": "r"}]),
        worker(objections=[{"check": "comment_block", "path": "a.py", "line": 1, "reason": ""}]),
        worker(
            objections=[{"check": "comment_block", "path": "a.py", "line": 1, "reason": "x" * 301}]
        ),
        worker(
            objections=[{"check": "comment_block", "path": "a\nb.py", "line": 1, "reason": "r"}]
        ),
        worker(objections=[{"check": "comment_block", "path": "a.py", "line": 1}]),
        worker(objections=[{"path": "a.py", "line": 1, "reason": "r"}]),
        worker(objections=[{"check": "protected_path", "path": "a.py", "line": 1, "reason": "r"}]),
        worker(objections=[{"check": "COMMENT_BLOCK", "path": "a.py", "line": 1, "reason": "r"}]),
        worker(
            objections=[
                {"check": "comment_block", "path": "a.py", "line": 1, "reason": "r", "skip": True}
            ]
        ),
        worker(
            objections=[{"check": "comment_block", "path": "a.py", "line": 1, "reason": "r"}] * 21
        ),
        worker(summary=5),
        json.dumps({"status": "done", "summary": "s"}),
        "",
        "not json",
        "[]",
        "null",
        '{"status": "done", "summary": "s", "pr_title": "t"} trailing',
        "```json\n" + worker() + "\n```",
    ],
)
def test_invalid_worker_output_fails_closed(raw: str) -> None:
    with pytest.raises(PolicyViolation, match="WorkerResult"):
        parse_output(WorkerResult, raw)


@pytest.mark.parametrize("status", ["blocked", "scope_too_large"])
def test_non_done_status_with_note_parses(status: str) -> None:
    assert parse_output(WorkerResult, worker(status=status, note="why")).note == "why"


def test_worker_with_new_issues_at_limit_parses() -> None:
    issues = [{"title": "t" * 120, "body": "b" * 4000}] * 10
    assert len(parse_output(WorkerResult, worker(new_issues=issues)).new_issues) == 10


def test_worker_objections_parse() -> None:
    raw = worker(
        objections=[{"check": "comment_block", "path": " app.py ", "line": 3, "reason": "License."}]
    )
    [objection] = parse_output(WorkerResult, raw).objections
    assert (objection.check, objection.path, objection.line, objection.reason) == (
        CheckId.COMMENT_BLOCK,
        "app.py",
        3,
        "License.",
    )


def test_models_are_frozen() -> None:
    result = parse_output(WorkerResult, worker())
    with pytest.raises(ValidationError):
        result.status = "blocked"


@pytest.mark.parametrize(
    "raw",
    [
        review(findings=[finding("blocker")]),
        review(findings=[finding("major")]),
        review(findings=[finding("minor"), finding("major")]),
        review(verdict="changes_requested", findings=[]),
        review(verdict="approved"),
        review(verdict="merge"),
        review(summary="x" * 1001),
        review(findings=[finding()] * 31),
        review(findings=[finding("critical")]),
        review(findings=[finding(line=0, path="a")]),
        review(findings=[finding(line=-1, path="a")]),
        review(findings=[finding(line=3)]),
        review(findings=[finding(line="3", path="a")]),
        review(findings=[finding(path="x" * 301)]),
        review(findings=[finding(path="a\nb")]),
        review(findings=[finding(message="")]),
        review(findings=[finding(message="x" * 501)]),
        review(findings=[finding(extra=1)]),
        review(missing_doc_updates=[doc()]),
        review(findings=[finding("minor")], missing_doc_updates=[doc()]),
        review(missing_doc_updates=None),
        review(missing_doc_updates=[doc()] * 21),
        review(verdict="changes_requested", missing_doc_updates=[doc(extra=1)]),
        review(verdict="changes_requested", missing_doc_updates=[doc(path="a\nb")]),
        review(verdict="changes_requested", missing_doc_updates=[doc(path="a\rb")]),
        review(verdict="changes_requested", missing_doc_updates=[doc(path="x" * 301)]),
        review(verdict="changes_requested", missing_doc_updates=[doc(path="")]),
        review(verdict="changes_requested", missing_doc_updates=[doc(reason="x" * 501)]),
        review(verdict="changes_requested", missing_doc_updates=[doc(reason=" ")]),
        review(verdict="changes_requested", missing_doc_updates=[{"path": "a"}]),
        review(verdict="changes_requested", missing_doc_updates=["docs/usage.md"]),
    ],
)
def test_invalid_review_output_fails_closed(raw: str) -> None:
    with pytest.raises(PolicyViolation, match="ReviewResult"):
        parse_output(ReviewResult, raw)


@pytest.mark.parametrize(
    "raw",
    [
        review(),
        review(findings=[finding("minor")]),
        review(verdict="changes_requested", findings=[finding("minor")]),
        review(verdict="changes_requested", findings=[finding("blocker", path="a", line=1)]),
        review(verdict="scope_too_large", findings=[]),
        review(verdict="scope_too_large", findings=[finding("blocker")]),
        review(verdict="changes_requested", missing_doc_updates=[doc()]),
        review(verdict="changes_requested", missing_doc_updates=[doc()] * 20),
        review(verdict="scope_too_large", missing_doc_updates=[doc()]),
    ],
)
def test_valid_review_output_parses(raw: str) -> None:
    assert isinstance(parse_output(ReviewResult, raw), ReviewResult)


def test_review_without_missing_doc_updates_fails_closed() -> None:
    raw = json.dumps({"verdict": "approve", "summary": "Fine.", "findings": []})
    with pytest.raises(PolicyViolation, match="missing_doc_updates"):
        parse_output(ReviewResult, raw)


def test_review_schema_requires_missing_doc_updates_and_forbids_extra_items() -> None:
    schema = json.loads(json_schema(ReviewResult))
    assert "missing_doc_updates" in schema["required"]
    assert schema["properties"]["missing_doc_updates"]["maxItems"] == 20
    item = schema["$defs"]["MissingDocUpdate"]
    assert item["additionalProperties"] is False
    assert sorted(item["required"]) == ["path", "reason"]


def test_refine_result_limits() -> None:
    ok = json.dumps({"spec_markdown": "# Spec", "open_questions": ["q1", "q2"]})
    assert parse_output(RefineResult, ok).open_questions == ["q1", "q2"]
    for bad in (
        {"spec_markdown": ""},
        {"spec_markdown": "x" * 6001},
        {"spec_markdown": "s", "open_questions": ["q"] * 11},
        {"spec_markdown": "s", "open_questions": [""]},
        {"spec_markdown": "s", "open_questions": ["x" * 301]},
        {"spec_markdown": "s", "comment_on": 12},
    ):
        with pytest.raises(PolicyViolation):
            parse_output(RefineResult, json.dumps(bad))


@pytest.mark.parametrize(
    "raw",
    [
        {"picks": [{"issue_number": 0, "reason": "r"}]},
        {"picks": [{"issue_number": -3, "reason": "r"}]},
        {"picks": [{"issue_number": "3", "reason": "r"}]},
        {"picks": [{"issue_number": 3.0, "reason": "r"}]},
        {"picks": [{"issue_number": True, "reason": "r"}]},
        {"picks": [{"issue_number": 3, "reason": ""}]},
        {"picks": [{"issue_number": 3, "reason": "x" * 301}]},
        {"picks": [{"issue_number": 3, "reason": "r"}] * 51},
        {"too_large": [{"issue_number": 3, "reason": "r"}] * 51},
    ],
)
def test_invalid_selection_output_fails_closed(raw: dict[str, Any]) -> None:
    with pytest.raises(PolicyViolation):
        parse_output(SelectionResult, json.dumps(raw))


def test_oversized_output_is_rejected_before_parsing() -> None:
    with pytest.raises(PolicyViolation, match="exceeds"):
        parse_output(WorkerResult, " " * (MAX_OUTPUT_CHARS + 1))


def test_violation_message_does_not_echo_agent_input() -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        parse_output(WorkerResult, worker(status="SECRET-LOOKING-VALUE"))
    assert "SECRET-LOOKING-VALUE" not in str(excinfo.value)
    assert "status" in str(excinfo.value)


def pick(number: int) -> SelectionPick:
    return SelectionPick(issue_number=number, reason="r")


def test_check_selection_truncates_picks_to_quota() -> None:
    result = SelectionResult(picks=[pick(1), pick(2), pick(3)], too_large=[pick(4)])
    checked = check_selection(result, {1, 2, 3, 4}, 2)
    assert checked == SelectionResult(picks=[pick(1), pick(2)], too_large=[pick(4)])


def test_check_selection_quota_zero_keeps_too_large() -> None:
    result = SelectionResult(picks=[pick(1)], too_large=[pick(2)])
    assert check_selection(result, [1, 2], 0) == SelectionResult(too_large=[pick(2)])


@pytest.mark.parametrize(
    ("picks", "too_large"),
    [
        ([99], []),
        ([], [99]),
        ([1, 1], []),
        ([], [2, 2]),
        ([1], [1]),
        ([1, 2, 99], []),
    ],
)
def test_check_selection_rejects_unknown_or_repeated_issues(
    picks: list[int], too_large: list[int]
) -> None:
    result = SelectionResult(picks=[pick(n) for n in picks], too_large=[pick(n) for n in too_large])
    with pytest.raises(PolicyViolation):
        check_selection(result, {1, 2, 3}, 10)


def test_check_selection_validates_beyond_the_quota() -> None:
    result = SelectionResult(picks=[pick(1), pick(99)])
    with pytest.raises(PolicyViolation):
        check_selection(result, {1}, 1)


def test_check_selection_rejects_negative_quota() -> None:
    with pytest.raises(PolicyViolation):
        check_selection(SelectionResult(), {1}, -1)


@pytest.mark.parametrize(
    "model",
    [
        WorkerResult,
        ReviewResult,
        RefineResult,
        SelectionResult,
        NewIssue,
        ReviewFinding,
        MissingDocUpdate,
    ],
)
def test_json_schema_is_compact_and_forbids_extra(model: type[BaseModel]) -> None:
    schema = json_schema(model)
    assert ": " not in schema
    assert ", " not in schema
    parsed = json.loads(schema)
    assert parsed["additionalProperties"] is False


def test_selection_rejects_issue_listed_as_pick_and_rejected() -> None:
    result = SelectionResult.model_validate(
        {
            "picks": [{"issue_number": 1, "reason": "r"}],
            "rejected": [{"issue_number": 1, "reason": "r"}],
        }
    )
    with pytest.raises(PolicyViolation):
        check_selection(result, [1], 3)


def test_selection_keeps_rejected_issues() -> None:
    result = SelectionResult.model_validate({"rejected": [{"issue_number": 2, "reason": "unsafe"}]})
    assert check_selection(result, [2], 3).rejected[0].issue_number == 2


@pytest.mark.parametrize("make", [worker, review])
@pytest.mark.parametrize(
    "blockers",
    [
        None,
        ["x" * 301],
        ["x" * 100_000],
        [""],
        ["  "],
        ["no node\nstatus: done"],
        ["tab\tseparated"],
        ["carriage\rreturn"],
        ["a"] * 11,
        [1],
        [{"text": "a"}],
        "Python is missing",
    ],
)
def test_invalid_blockers_fail_closed(make: Any, blockers: Any) -> None:
    model = WorkerResult if make is worker else ReviewResult
    with pytest.raises(PolicyViolation, match=model.__name__):
        parse_output(model, make(blockers=blockers))


@pytest.mark.parametrize("model", [WorkerResult, ReviewResult])
def test_blockers_are_required(model: type[BaseModel]) -> None:
    data = json.loads(worker() if model is WorkerResult else review())
    del data["blockers"]
    with pytest.raises(PolicyViolation, match="blockers"):
        parse_output(model, json.dumps(data))
    schema = json.loads(json_schema(model))
    assert "blockers" in schema["required"]
    assert schema["properties"]["blockers"]["maxItems"] == 10
    assert schema["properties"]["blockers"]["items"]["maxLength"] == 300


def test_blockers_parse_stripped_at_the_limit() -> None:
    blockers = [" Python 3.13 is missing "] + ["x" * 300] * 9
    result = parse_output(WorkerResult, worker(blockers=blockers))
    assert result.blockers[0] == "Python 3.13 is missing"
    assert len(result.blockers) == 10
    review_result = parse_output(ReviewResult, review(blockers=["pypi.org is blocked"]))
    assert review_result.blockers == ["pypi.org is blocked"]
