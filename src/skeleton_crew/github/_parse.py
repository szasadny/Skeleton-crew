from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from githubkit import Response

from skeleton_crew.domain.ci_policy import BranchRule
from skeleton_crew.domain.trust_policy import ContentEdit, IssueComment, LabelEvent

PER_PAGE = 100
EDITS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String, $first: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      userContentEdits(first: $first, after: $cursor) {
        nodes { editedAt editor { login } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

Extract = Callable[[Response[Any, Any]], list[Any]]


def json_list(resp: Response[Any, Any]) -> list[Any]:
    data = resp.json()
    if not isinstance(data, list):
        raise TypeError("expected a JSON list")
    return data


def key_list(key: str) -> Extract:
    def extract(resp: Response[Any, Any]) -> list[Any]:
        data = resp.json().get(key)
        if not isinstance(data, list):
            raise TypeError(f"expected a JSON list in {key!r}")
        return data

    return extract


def login(user: Any) -> str:
    if isinstance(user, Mapping) and isinstance(user.get("login"), str):
        return str(user["login"])
    return ""


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def label_event(event: Mapping[str, Any]) -> LabelEvent:
    return LabelEvent(
        label=str(event["label"]["name"]),
        actor=login(event.get("actor")),
        at=timestamp(event["created_at"]),
        added=event.get("event") == "labeled",
    )


def rename_edit(event: Mapping[str, Any]) -> ContentEdit:
    return ContentEdit(login(event.get("actor")), timestamp(event["created_at"]))


def body_edit(node: Mapping[str, Any]) -> ContentEdit:
    return ContentEdit(login(node.get("editor")), timestamp(node["editedAt"]))


def issue_comment(raw: Mapping[str, Any]) -> IssueComment:
    return IssueComment(
        login(raw.get("user")), str(raw.get("body") or ""), timestamp(raw["created_at"])
    )


def branch_rule(raw: Mapping[str, Any]) -> BranchRule:
    return BranchRule(
        type=str(raw.get("type") or ""),
        ruleset_id=raw.get("ruleset_id"),
        parameters=raw.get("parameters") or {},
    )
