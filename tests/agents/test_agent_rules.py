import pytest

from skeleton_crew.agents.rules import compose_rules, default_rules
from skeleton_crew.domain.settings import RULE_KEYS

_MAX_LINES = 40


@pytest.mark.parametrize("key", RULE_KEYS)
def test_default_rules_load_and_stay_short(key: str) -> None:
    text = default_rules(key)
    assert text.strip()
    assert len(text.splitlines()) <= _MAX_LINES


def test_common_rules_cover_the_non_negotiables() -> None:
    text = default_rules("common").lower()
    for phrase in ("never ask questions", "nobody is available", "never wait", "claude.md"):
        assert phrase in text
    for phrase in ("agents.md", "protected paths", "one short line", "only the json"):
        assert phrase in text


def test_worker_rules_forbid_pushing() -> None:
    text = default_rules("worker")
    assert "git push" in text
    assert "`gh`" in text


def test_reviewer_rules_require_the_missing_doc_updates_list() -> None:
    text = default_rules("reviewer")
    assert "`missing_doc_updates`" in text
    assert "no missing doc updates" in text


def test_unknown_rules_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown rules key"):
        default_rules("../../etc/passwd")


def test_compose_puts_common_rules_first() -> None:
    assert compose_rules("common\n", "\nrole") == "common\n\nrole\n"
