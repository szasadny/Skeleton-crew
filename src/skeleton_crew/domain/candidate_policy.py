from collections.abc import Iterable
from enum import StrEnum

from skeleton_crew.domain.label_policy import has_label
from skeleton_crew.domain.settings import LabelNames


class PrRoute(StrEnum):
    NEW_WORK = "new_work"
    MAINTENANCE = "maintenance"
    AMBIGUOUS = "ambiguous"


def wants_work(labels: Iterable[str], names: LabelNames) -> bool:
    present = list(labels)
    return not has_label(present, names.hitl) and not has_label(present, names.refine)


def wants_refine(labels: Iterable[str], names: LabelNames) -> bool:
    return not has_label(labels, names.hitl)


def pr_route(open_prs: int) -> PrRoute:
    if open_prs == 0:
        return PrRoute.NEW_WORK
    if open_prs == 1:
        return PrRoute.MAINTENANCE
    return PrRoute.AMBIGUOUS
