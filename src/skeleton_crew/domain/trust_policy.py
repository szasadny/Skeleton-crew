from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from skeleton_crew.domain.label_policy import same_label

TRUSTED_PERMISSIONS = frozenset({"admin", "maintain", "write"})
_BOT_SUFFIX = "[bot]"


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


def always_trusted(extra_trusted: Iterable[str], bot_login: str | None) -> frozenset[str]:
    logins = [*extra_trusted, *([bot_login] if bot_login else [])]
    return frozenset(login.casefold() for login in logins)


def never_trusted(login: str) -> bool:
    return not login or login.endswith(_BOT_SUFFIX)


def trusted_role(role: str) -> bool:
    return role in TRUSTED_PERMISSIONS


def trusted_label_time(
    events: Sequence[LabelEvent], label: str, is_trusted: Callable[[str], bool]
) -> datetime | None:
    relevant = [e for e in events if same_label(e.label, label)]
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
