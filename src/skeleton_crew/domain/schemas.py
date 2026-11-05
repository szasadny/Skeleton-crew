import json
import unicodedata
from collections.abc import Collection
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from skeleton_crew.domain.contest_policy import CheckId
from skeleton_crew.domain.errors import PolicyViolation

MAX_OUTPUT_CHARS = 200_000


def _single_line(value: str) -> str:
    if any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError("must be a single line without control characters")
    return value


Title = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
    AfterValidator(_single_line),
]
PrTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=72),
    AfterValidator(_single_line),
]
Text300 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Text500 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Text1000 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
Text1500 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)]
Text4000 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Text6000 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]
FindingPath = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
    AfterValidator(_single_line),
]


class _AgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class NewIssue(_AgentOutput):
    title: Title
    body: Text4000


class Objection(_AgentOutput):
    check: CheckId
    path: FindingPath
    line: int = Field(ge=1)
    reason: Text300


class WorkerResult(_AgentOutput):
    status: Literal["done", "scope_too_large", "blocked"]
    summary: Text1500
    pr_title: PrTitle
    note: Text1500 | None = None
    new_issues: list[NewIssue] = Field(default_factory=list, max_length=10)
    objections: list[Objection] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _note_explains_non_done(self) -> Self:
        if self.status != "done" and self.note is None:
            raise ValueError("note is required when status is not 'done'")
        return self


class ReviewFinding(_AgentOutput):
    severity: Literal["blocker", "major", "minor"]
    path: FindingPath | None = None
    line: int | None = Field(default=None, ge=1)
    message: Text500

    @model_validator(mode="after")
    def _line_needs_path(self) -> Self:
        if self.line is not None and self.path is None:
            raise ValueError("line requires path")
        return self


class MissingDocUpdate(_AgentOutput):
    path: FindingPath
    reason: Text500


class ReviewResult(_AgentOutput):
    verdict: Literal["approve", "changes_requested", "scope_too_large"]
    summary: Text1000
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=30)
    missing_doc_updates: list[MissingDocUpdate] = Field(max_length=20)

    @model_validator(mode="after")
    def _verdict_matches_findings(self) -> Self:
        if self.verdict == "approve" and any(f.severity != "minor" for f in self.findings):
            raise ValueError("approve is not allowed with blocker or major findings")
        if self.verdict == "approve" and self.missing_doc_updates:
            raise ValueError("approve is not allowed with missing doc updates")
        if self.verdict == "changes_requested" and not (self.findings or self.missing_doc_updates):
            raise ValueError("changes_requested needs a finding or a missing doc update")
        return self


class RefineResult(_AgentOutput):
    spec_markdown: Text6000
    open_questions: list[Text300] = Field(default_factory=list, max_length=10)


class SelectionPick(_AgentOutput):
    issue_number: int = Field(ge=1)
    reason: Text300


class SelectionResult(_AgentOutput):
    picks: list[SelectionPick] = Field(default_factory=list, max_length=50)
    too_large: list[SelectionPick] = Field(default_factory=list, max_length=50)
    rejected: list[SelectionPick] = Field(default_factory=list, max_length=50)


def json_schema(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), separators=(",", ":"), sort_keys=True)


def check_selection(
    result: SelectionResult, candidates: Collection[int], quota: int
) -> SelectionResult:
    if quota < 0:
        raise PolicyViolation(f"invalid quota: {quota}")
    numbers = [p.issue_number for p in (*result.picks, *result.too_large, *result.rejected)]
    if len(set(numbers)) != len(numbers):
        raise PolicyViolation("selection lists an issue more than once")
    unknown = sorted(set(numbers) - set(candidates))
    if unknown:
        raise PolicyViolation(f"selection lists unknown issues: {unknown}")
    return SelectionResult(
        picks=result.picks[:quota], too_large=result.too_large, rejected=result.rejected
    )


def parse_output[T: BaseModel](model: type[T], raw: str) -> T:
    if len(raw) > MAX_OUTPUT_CHARS:
        raise PolicyViolation(f"{model.__name__} output exceeds {MAX_OUTPUT_CHARS} characters")
    try:
        return model.model_validate_json(raw)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
            for e in error.errors(include_url=False, include_input=False, include_context=False)
        )
        raise PolicyViolation(f"invalid {model.__name__} output: {details}") from None
