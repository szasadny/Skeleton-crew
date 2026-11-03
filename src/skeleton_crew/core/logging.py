import logging
from collections.abc import Sequence

REDACTED = "***"
_MIN_SECRET_CHARS = 8


def _effective_secrets(secrets: Sequence[str]) -> list[str]:
    # Longest first so a secret that contains another is fully masked.
    return sorted({s for s in secrets if len(s) >= _MIN_SECRET_CHARS}, key=len, reverse=True)


def _replace_all(text: str, ordered_secrets: Sequence[str]) -> str:
    for secret in ordered_secrets:
        text = text.replace(secret, REDACTED)
    return text


def redact(text: str, secrets: Sequence[str]) -> str:
    return _replace_all(text, _effective_secrets(secrets))


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Sequence[str]) -> None:
        super().__init__()
        self._secrets = _effective_secrets(secrets)

    def redact(self, text: str) -> str:
        return _replace_all(text, self._secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


def configure_logging(secrets: Sequence[str], level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter(secrets))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=level, handlers=[handler], force=True)
