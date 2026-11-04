import pytest

from skeleton_crew.domain.task_keys import (
    KeyKind,
    is_task_key,
    parse_task_key,
    refine_key,
    selector_key,
    worker_key,
)


def test_keys_have_stable_formats() -> None:
    assert worker_key(7) == "worker-7"
    assert refine_key(8) == "refine-8"
    assert selector_key(9) == "run-9-selector"


@pytest.mark.parametrize("key", [worker_key(1), refine_key(22), selector_key(333)])
def test_generated_keys_are_task_keys(key: str) -> None:
    assert is_task_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "worker-",
        "worker-1/..",
        "../worker-1",
        "worker-1\n",
        "Worker-1",
        "worker-\u0661",
        "run-1",
        "run-1-selector-2",
        "selector-1",
        "tasks",
        "worker--1",
    ],
)
def test_other_strings_are_not_task_keys(key: str) -> None:
    assert not is_task_key(key)
    assert parse_task_key(key) is None


@pytest.mark.parametrize(
    ("key", "parsed"),
    [
        (worker_key(7), (KeyKind.WORKER, 7)),
        (refine_key(8), (KeyKind.REFINE, 8)),
        (selector_key(9), (KeyKind.SELECTOR, 9)),
        ("worker-007", (KeyKind.WORKER, 7)),
    ],
)
def test_parse_task_key_returns_kind_and_id(key: str, parsed: tuple[KeyKind, int]) -> None:
    assert parse_task_key(key) == parsed


@pytest.mark.parametrize(
    "key",
    [
        "worker-\u0661",
        "refine-\uff11",
        "run-\u0663-selector",
        "worker-1 ",
        " refine-2",
        "run--1-selector",
    ],
)
def test_parse_task_key_rejects_malformed_and_non_ascii(key: str) -> None:
    assert parse_task_key(key) is None
