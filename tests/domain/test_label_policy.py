import pytest

from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.label_policy import (
    LabelChange,
    check_label_change,
    check_new_issue_labels,
    escalation_change,
    label_key,
    refine_done_change,
)
from skeleton_crew.domain.settings import LabelNames

LABELS = LabelNames()


def change(add: set[str] | None = None, remove: set[str] | None = None) -> LabelChange:
    return LabelChange(7, frozenset(add or set()), frozenset(remove or set()))


@pytest.mark.parametrize(
    ("add", "remove"),
    [
        ({"skeleton HITL"}, set()),
        ({"skeleton hitl"}, set()),
        ({"SKELETON HITL"}, set()),
        (set(), {"skeleton"}),
        (set(), {"Skeleton"}),
        (set(), {"skeleton refine"}),
        (set(), {"SKELETON REFINE"}),
        (set(), {"skeleton", "skeleton refine"}),
        ({"skeleton HITL"}, {"skeleton"}),
        ({"skeleton HITL"}, {"skeleton refine"}),
    ],
)
def test_allowed_changes_pass(add: set[str], remove: set[str]) -> None:
    check_label_change(change(add, remove), LABELS)


@pytest.mark.parametrize(
    "label",
    [
        "skeleton",
        "Skeleton",
        "SKELETON",
        " skeleton",
        "skeleton ",
        "skeleton\t",
        "\nskeleton",
        "skeleton refine",
        "Skeleton Refine",
        "skeleton  refine",
        " skeleton refine ",
        "\u017fkeleton",
        "s\u212aeleton",
        "\uff53\uff4b\uff45\uff4c\uff45\uff54\uff4f\uff4e",
        "skeleton\u200b",
        "\u200bskeleton",
        "skel\u00adeton",
        "skeleton\x00",
        "bug",
        "",
        "skeleton HITL ",
        " skeleton HITL",
        "skeleton\u00a0HITL",
        "\u017fkeleton HITL",
        "\u0405keleton HITL",
        "skeleton HITL\u2028",
    ],
)
def test_adding_anything_but_hitl_is_rejected(label: str) -> None:
    with pytest.raises(PolicyViolation):
        check_label_change(change(add={label}), LABELS)


@pytest.mark.parametrize(
    "label",
    ["skeleton HITL", "SKELETON hitl", "bug", "wontfix", "skeleton-refine", "", " skeleton"],
)
def test_removing_anything_but_ready_or_refine_is_rejected(label: str) -> None:
    with pytest.raises(PolicyViolation):
        check_label_change(change(remove={label}), LABELS)


def test_adding_hitl_alongside_forbidden_label_is_rejected() -> None:
    with pytest.raises(PolicyViolation):
        check_label_change(change(add={"skeleton HITL", "skeleton"}), LABELS)


def test_removing_allowed_alongside_forbidden_label_is_rejected() -> None:
    with pytest.raises(PolicyViolation):
        check_label_change(change(remove={"skeleton", "skeleton HITL"}), LABELS)


def test_empty_change_is_rejected() -> None:
    with pytest.raises(PolicyViolation, match="empty"):
        check_label_change(change(), LABELS)


def test_add_and_remove_overlap_is_rejected() -> None:
    labels = LabelNames(ready="a", refine="b", hitl="c")
    with pytest.raises(PolicyViolation):
        check_label_change(change(add={"c"}, remove={"C"}), labels)


def test_case_variants_of_one_label_in_a_change_are_rejected() -> None:
    with pytest.raises(PolicyViolation, match="duplicate"):
        check_label_change(change(remove={"skeleton", "SKELETON"}), LABELS)


@pytest.mark.parametrize("number", [0, -1])
def test_invalid_issue_number_is_rejected(number: int) -> None:
    with pytest.raises(PolicyViolation):
        check_label_change(LabelChange(number, frozenset({"skeleton HITL"}), frozenset()), LABELS)


def test_custom_label_names_are_respected() -> None:
    labels = LabelNames(ready="ai:ready", refine="ai:refine", hitl="ai:human")
    check_label_change(change(add={"AI:Human"}, remove={"ai:ready"}), labels)
    with pytest.raises(PolicyViolation):
        check_label_change(change(add={"skeleton HITL"}), labels)
    with pytest.raises(PolicyViolation):
        check_label_change(change(add={"ai:ready"}), labels)


def test_escalation_change_swaps_ready_for_hitl() -> None:
    assert escalation_change(3, LABELS) == LabelChange(
        3, frozenset({"skeleton HITL"}), frozenset({"skeleton"})
    )


def test_refine_done_change_swaps_refine_for_hitl() -> None:
    assert refine_done_change(3, LABELS) == LabelChange(
        3, frozenset({"skeleton HITL"}), frozenset({"skeleton refine"})
    )


@pytest.mark.parametrize("factory", [escalation_change, refine_done_change])
def test_builders_reject_invalid_issue_numbers(factory: object) -> None:
    assert callable(factory)
    with pytest.raises(PolicyViolation):
        factory(0, LABELS)


@pytest.mark.parametrize("labels", [{"skeleton HITL"}, {"Skeleton hitl"}])
def test_new_issue_with_only_hitl_is_allowed(labels: set[str]) -> None:
    check_new_issue_labels(frozenset(labels), LABELS)


@pytest.mark.parametrize(
    "labels",
    [
        set(),
        {"skeleton"},
        {"skeleton refine"},
        {"skeleton HITL", "skeleton"},
        {"skeleton HITL", "bug"},
        {"skeleton HITL", "SKELETON HITL"},
        {"skeleton HITL "},
        {"\u017fkeleton HITL"},
    ],
)
def test_new_issue_with_other_labels_is_rejected(labels: set[str]) -> None:
    with pytest.raises(PolicyViolation):
        check_new_issue_labels(frozenset(labels), LABELS)


@pytest.mark.parametrize("name", ["ok", "Ünïcode", "with space", "emoji 🐛"])
def test_label_key_accepts_normal_names(name: str) -> None:
    assert label_key(name) == name.casefold()
