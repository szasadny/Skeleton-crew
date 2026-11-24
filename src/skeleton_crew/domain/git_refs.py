import re

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def is_full_sha(sha: str) -> bool:
    return _FULL_SHA_RE.fullmatch(sha) is not None
