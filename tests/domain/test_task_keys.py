import pytest

from skeleton_crew.domain.task_keys import (
    COMMAND_SUFFIX,
    SESSION_SUFFIX,
    TRANSCRIPTS_DIR,
    KeyKind,
    RepoCommand,
    container_name,
    is_task_key,
    is_transcript_name,
    name_number,
    numbered_name,
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


def test_container_names_carry_prefix_key_role_and_nonce() -> None:
    assert container_name("sc-", worker_key(3), "reviewer", "ab12") == "sc-worker-3-reviewer-ab12"


@pytest.mark.parametrize(
    "name",
    ["worker-1.jsonl", "reviewer-12.jsonl", "selector-3.jsonl", "setup-1.log", "check-40.log"],
)
def test_transcript_names_of_sessions_and_commands(name: str) -> None:
    assert is_transcript_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "worker-1.json",
        "worker-.jsonl",
        "worker-x.jsonl",
        "Worker-1.jsonl",
        "worker-1.jsonl\n",
        "worker-1.jsonl\x00",
        "worker-1.jsonl.bak",
        "build-1.log",
        "setup-1.jsonl.log",
        "worker-1.log",
        "precheck-1.log",
        "../worker-1.jsonl",
        "transcripts/worker-1.jsonl",
        "worker-1.jsonl/..",
        "/etc/worker-1.jsonl",
        "\\\\host\\worker-1.jsonl",
        "..",
        "worker-\u0661.jsonl",
        "wörker-1.jsonl",
        "worker_1-1.jsonl",
        "a" * 5000 + "-1.log",
    ],
)
def test_hostile_or_foreign_names_are_no_transcripts(name: str) -> None:
    assert not is_transcript_name(name)


def test_long_session_names_stay_valid_but_bounded_by_the_grammar() -> None:
    assert is_transcript_name("a" * 5000 + "-1.jsonl")


def test_built_names_round_trip() -> None:
    assert numbered_name("worker", 3, SESSION_SUFFIX) == "worker-3.jsonl"
    assert name_number("worker-3.jsonl", "worker", SESSION_SUFFIX) == 3
    assert name_number("worker-3.jsonl", "work", SESSION_SUFFIX) is None
    assert name_number("check-2.log.bak", RepoCommand.CHECK, COMMAND_SUFFIX) is None
    for command in RepoCommand:
        assert is_transcript_name(numbered_name(command, 1, COMMAND_SUFFIX))
    assert TRANSCRIPTS_DIR == "transcripts"
