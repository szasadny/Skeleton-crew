import pytest

from skeleton_crew.domain.candidate_policy import PrRoute, pr_route, wants_refine, wants_work
from skeleton_crew.domain.settings import LabelNames

NAMES = LabelNames()


def test_a_ready_issue_wants_work() -> None:
    assert wants_work(["skeleton", "bug"], NAMES)


@pytest.mark.parametrize(
    "labels",
    [
        ["skeleton", "skeleton HITL"],
        ["skeleton", "skeleton refine"],
        ["skeleton", "Skeleton hitl"],
        iter(["skeleton", "skeleton HITL"]),
    ],
)
def test_hitl_or_refine_keeps_an_issue_from_work(labels: list[str]) -> None:
    assert not wants_work(labels, NAMES)


def test_hitl_keeps_an_issue_from_refine() -> None:
    assert wants_refine(["skeleton refine"], NAMES)
    assert not wants_refine(["skeleton refine", "skeleton HITL"], NAMES)


@pytest.mark.parametrize(
    ("count", "route"),
    [
        (0, PrRoute.NEW_WORK),
        (1, PrRoute.MAINTENANCE),
        (2, PrRoute.AMBIGUOUS),
        (9, PrRoute.AMBIGUOUS),
    ],
)
def test_one_open_pr_is_maintenance_and_several_are_skipped(count: int, route: PrRoute) -> None:
    assert pr_route(count) is route
