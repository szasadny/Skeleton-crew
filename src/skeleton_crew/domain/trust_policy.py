from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

TRUSTED_PERMISSIONS = frozenset({"admin", "maintain", "write"})


@dataclass(frozen=True)
class LabelEvent:
    label: str
    actor: str
    at: datetime
    added: bool


@dataclass(frozen=True)
class ContentEdit:
    editor: str
    at: datetime


@dataclass(frozen=True)
class IssueComment:
    author: str
    body: str
    at: datetime


def trusted_label_time(
    events: Sequence[LabelEvent], label: str, is_trusted: Callable[[str], bool]
) -> datetime | None:
    relevant = [e for e in events if e.label.casefold() == label.casefold()]
    if not relevant:
        return None
    latest_at = max(e.at for e in relevant)
    # Simultaneous events have no defined order, so every one of them must be a trusted add.
    latest = [e for e in relevant if e.at == latest_at]
    if all(e.added and is_trusted(e.actor) for e in latest):
        return latest_at
    return None


def content_untampered(
    edits: Sequence[ContentEdit], since: datetime, is_trusted: Callable[[str], bool]
) -> bool:
    return all(is_trusted(e.editor) for e in edits if e.at >= since)


def trusted_comments(
    comments: Sequence[IssueComment], is_trusted: Callable[[str], bool]
) -> list[IssueComment]:
    return [c for c in comments if is_trusted(c.author)]
