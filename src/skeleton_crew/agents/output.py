import json
import re

_FENCED_JSON_RE = re.compile(r"\A```json[ \t]*\n(?P<body>.*?)\n```\Z", re.DOTALL)


def _as_object(text: str) -> str | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return json.dumps(value) if isinstance(value, dict) else None


def extract_json_text(answer: str) -> str | None:
    text = answer.strip()
    if text.startswith("{"):
        return _as_object(text)
    match = _FENCED_JSON_RE.fullmatch(text)
    if match is None or "```" in match["body"]:
        return None
    return _as_object(match["body"])
