import pytest

from skeleton_crew.domain.task_keys import is_task_key, refine_key, selector_key, worker_key


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
