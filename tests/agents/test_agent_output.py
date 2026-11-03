import pytest

from skeleton_crew.agents.output import extract_json_text


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('  \n{"a": [1, 2]}\n', '{"a": [1, 2]}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('\n```json\n{\n  "a": 1\n}\n```\n', '{"a": 1}'),
    ],
)
def test_extracts_a_whole_or_single_fenced_object(answer: str, expected: str) -> None:
    assert extract_json_text(answer) == expected


@pytest.mark.parametrize(
    "answer",
    [
        "",
        "Done.",
        'Result: {"a": 1}',
        '{"a": 1} trailing',
        "[1, 2]",
        '"text"',
        '```\n{"a": 1}\n```',
        '```json\n{"a": 1}\n```\nmore text',
        '```json\n{"a": 1}\n```\n```json\n{"b": 2}\n```',
        '```json\n{"a": 1\n```',
    ],
)
def test_rejects_anything_else(answer: str) -> None:
    assert extract_json_text(answer) is None
