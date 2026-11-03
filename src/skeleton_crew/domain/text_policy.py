import re
import unicodedata

TRUNCATION_SUFFIX = "\n\n… (truncated)"
_ZWSP = "\u200b"
# Trojan Source: bidi embeddings, overrides and isolates reorder rendered text.
_BIDI_CONTROLS = frozenset("\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_MENTION_RE = re.compile(r"@(?=[A-Za-z0-9])")
_EMAIL_TAIL_RE = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_CLOSING_RE = re.compile(
    r"(?<![A-Za-z0-9])(close[sd]?|fix(?:es|ed)?|resolve[sd]?):?\s*"
    r"(?=#\d|[\w.-]+/[\w.-]+#\d|gh-\d|https?://|\S*/issues/\d)",
    re.IGNORECASE,
)


def _strip_controls(text: str) -> str:
    return "".join(
        c
        for c in text
        if c in "\n\t" or (unicodedata.category(c) != "Cc" and c not in _BIDI_CONTROLS)
    )


def _neutralise_mention(match: re.Match[str]) -> str:
    text, at = match.string, match.start()
    is_email = at > 0 and text[at - 1].isascii() and text[at - 1].isalnum()
    if is_email and _EMAIL_TAIL_RE.match(text, at + 1):
        return "@"
    return "@" + _ZWSP


def _neutralise_keyword(match: re.Match[str]) -> str:
    word = match.group(0)
    return word[0] + _ZWSP + word[1:]


def truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= len(TRUNCATION_SUFFIX):
        raise ValueError("max_chars must exceed the truncation suffix")
    if len(text) > max_chars:
        return text[: max_chars - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX
    return text


def sanitize_agent_text(text: str, max_chars: int) -> str:
    cleaned = _strip_controls(text)
    cleaned = _MENTION_RE.sub(_neutralise_mention, cleaned)
    cleaned = _CLOSING_RE.sub(_neutralise_keyword, cleaned)
    return truncate_text(cleaned, max_chars)
