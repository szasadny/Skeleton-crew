import unicodedata
from dataclasses import dataclass

from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.settings import LabelNames


@dataclass(frozen=True)
class LabelChange:
    issue_number: int
    add: frozenset[str]
    remove: frozenset[str]


def label_key(name: str) -> str:
    # Compatibility lookalikes and padded names could otherwise fold onto a protected label.
    if (
        not name
        or name != name.strip()
        or unicodedata.normalize("NFKC", name) != name
        or any(unicodedata.category(c).startswith("C") for c in name)
    ):
        raise PolicyViolation(f"label name not allowed: {name!r}")
    return name.casefold()


def _keys(names: frozenset[str]) -> set[str]:
    return {label_key(n) for n in names}


def check_label_change(change: LabelChange, labels: LabelNames) -> None:
    if change.issue_number < 1:
        raise PolicyViolation(f"invalid issue number: {change.issue_number}")
    if not change.add and not change.remove:
        raise PolicyViolation("empty label change")
    add, remove = _keys(change.add), _keys(change.remove)
    if len(add) != len(change.add) or len(remove) != len(change.remove):
        raise PolicyViolation("duplicate labels in change")
    if add & remove:
        raise PolicyViolation("label both added and removed")
    if not add <= {label_key(labels.hitl)}:
        raise PolicyViolation(f"adding labels not allowed: {sorted(change.add)}")
    if not remove <= {label_key(labels.ready), label_key(labels.refine)}:
        raise PolicyViolation(f"removing labels not allowed: {sorted(change.remove)}")


def check_new_issue_labels(labels_to_set: frozenset[str], labels: LabelNames) -> None:
    if len(labels_to_set) != 1 or _keys(labels_to_set) != {label_key(labels.hitl)}:
        raise PolicyViolation(f"new issue labels not allowed: {sorted(labels_to_set)}")


def escalation_change(issue_number: int, labels: LabelNames) -> LabelChange:
    change = LabelChange(issue_number, frozenset({labels.hitl}), frozenset({labels.ready}))
    check_label_change(change, labels)
    return change


def refine_done_change(issue_number: int, labels: LabelNames) -> LabelChange:
    change = LabelChange(issue_number, frozenset({labels.hitl}), frozenset({labels.refine}))
    check_label_change(change, labels)
    return change
