import re

import pytest

from skeleton_crew.domain.text_policy import TRUNCATION_SUFFIX, sanitize_agent_text, truncate_text

ZWSP = "\u200b"
LIMIT = 10_000
_LIVE_MENTION = re.compile(r"(?<![A-Za-z0-9])@[A-Za-z0-9]")
_LIVE_KEYWORD = re.compile(
    r"(?<![A-Za-z0-9])(close[sd]?|fix(es|ed)?|resolve[sd]?):?\s*(#\d|[\w.-]+/[\w.-]+#\d|https?://)",
    re.IGNORECASE,
)


def clean(text: str) -> str:
    return sanitize_agent_text(text, LIMIT)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ping @octocat", f"ping @{ZWSP}octocat"),
        ("@octocat at start", f"@{ZWSP}octocat at start"),
        ("(@octocat)", f"(@{ZWSP}octocat)"),
        ("cc @org/team", f"cc @{ZWSP}org/team"),
        ("`@octocat` in code", f"`@{ZWSP}octocat` in code"),
        ("```\n@octocat\n```", f"```\n@{ZWSP}octocat\n```"),
        ("@a @b", f"@{ZWSP}a @{ZWSP}b"),
        ("@@octocat", f"@@{ZWSP}octocat"),
        ("x_@octocat", f"x_@{ZWSP}octocat"),
        ("x.@octocat", f"x.@{ZWSP}octocat"),
        ("word@octocat", f"word@{ZWSP}octocat"),
        ("é@octocat.com", f"é@{ZWSP}octocat.com"),
        ("@1user", f"@{ZWSP}1user"),
        ("@Octo-Cat", f"@{ZWSP}Octo-Cat"),
        ("@\u200boctocat", "@\u200boctocat"),
        ("@ octocat", "@ octocat"),
        ("@-octocat", "@-octocat"),
        ("@", "@"),
    ],
)
def test_mentions_are_neutralised(text: str, expected: str) -> None:
    assert clean(text) == expected


@pytest.mark.parametrize(
    "text",
    ["mail dev@example.com", "a@b.c", "first.last+tag@sub.example.org", "x1@host-name.io"],
)
def test_email_addresses_are_left_alone(text: str) -> None:
    assert clean(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "Closes #3",
        "closes #3",
        "CLOSED #3",
        "close #3",
        "Fix #3",
        "fixes #3",
        "Fixed #3",
        "Resolve #3",
        "resolves #3",
        "RESOLVED #3",
        "Fixes: #3",
        "Fixes:#3",
        "fixes#3",
        "Fixes  \t #3",
        "Fixes\n#3",
        "Fixes\u00a0#3",
        "RESOLVED owner/repo#9",
        "closes my-org/my.repo#12",
        "fixes https://github.com/owner/repo/issues/4",
        "Fixes http://ghe.example.com/owner/repo/issues/4",
        "fixes github.com/owner/repo/issues/4",
        "Fixes GH-4",
        "(fixes #3)",
        "_fixes #3",
        "see #2. Fixes #3",
    ],
)
def test_closing_keywords_are_neutralised(text: str) -> None:
    result = clean(text)
    assert _LIVE_KEYWORD.search(result) is None
    assert ZWSP in result
    assert result.replace(ZWSP, "") == text


def test_keyword_neutralised_after_first_letter() -> None:
    assert clean("Fixes #3") == f"F{ZWSP}ixes #3"


def test_every_keyword_in_a_list_is_neutralised() -> None:
    result = clean("closes #1, fixes #2 and resolves owner/r#3")
    assert result.count(ZWSP) == 3
    assert _LIVE_KEYWORD.search(result) is None


@pytest.mark.parametrize(
    "text",
    [
        "Prefixes #3",
        "suffixes #3",
        "unresolved #3",
        "enclosed #3",
        "fixing #3",
        "fix the bug",
        "closes the door",
        "issue #3 is related",
        "fixes",
    ],
)
def test_non_closing_text_is_left_alone(text: str) -> None:
    assert clean(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a\x00b", "ab"),
        ("a\x07b\x1bc\x7f", "abc"),
        ("line1\r\nline2", "line1\nline2"),
        ("tab\tkept\nnewline kept", "tab\tkept\nnewline kept"),
        ("x\u202eevil\u202c", "xevil"),
        ("\u2066iso\u2069", "iso"),
        ("\u200erlm\u200f", "rlm"),
        ("a\x85b", "ab"),
    ],
)
def test_control_and_bidi_characters_are_stripped(text: str, expected: str) -> None:
    assert clean(text) == expected


def test_stripping_controls_cannot_create_a_live_mention() -> None:
    result = clean("@\x00octocat and F\x00ixes #3")
    assert _LIVE_MENTION.search(result) is None
    assert _LIVE_KEYWORD.search(result) is None


def test_short_text_is_not_truncated() -> None:
    assert sanitize_agent_text("x" * 100, 100) == "x" * 100


def test_long_text_is_truncated_to_exact_limit() -> None:
    result = sanitize_agent_text("x" * 101, 100)
    assert len(result) == 100
    assert result.endswith(TRUNCATION_SUFFIX)
    assert result.startswith("x" * (100 - len(TRUNCATION_SUFFIX)))


def test_limit_applies_after_neutralisation() -> None:
    text = "@a " * 40
    result = sanitize_agent_text(text, 120)
    assert len(result) <= 120
    assert _LIVE_MENTION.search(result) is None


@pytest.mark.parametrize("cut", range(40, 60))
def test_truncation_never_leaves_a_live_mention_or_keyword(cut: int) -> None:
    text = "intro text here. Fixes #12 then @octocat and owner/repo " * 3
    result = sanitize_agent_text(text, cut)
    assert len(result) <= cut
    assert _LIVE_MENTION.search(result) is None
    assert _LIVE_KEYWORD.search(result) is None


def test_limit_must_exceed_suffix() -> None:
    with pytest.raises(ValueError, match="suffix"):
        sanitize_agent_text("x", len(TRUNCATION_SUFFIX))


def test_sanitising_twice_is_stable() -> None:
    once = clean("Fixes #3 @octocat a@b.c")
    assert clean(once) == once


def test_truncate_text_caps_without_sanitising() -> None:
    assert truncate_text("@alice", 100) == "@alice"
    capped = truncate_text("x" * 500, 100)
    assert len(capped) == 100
    assert capped.endswith(TRUNCATION_SUFFIX)
    with pytest.raises(ValueError, match="suffix"):
        truncate_text("x", len(TRUNCATION_SUFFIX))
