import logging
from collections.abc import Iterator

import pytest

from skeleton_crew.core.logging import RedactingFilter, redact

_SECRET = "tok-abcdef123456"  # noqa: S105


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            self.format(record)
            captured.append(record)

    handler = _Capture()
    handler.addFilter(RedactingFilter([_SECRET, "short"]))
    logger = logging.getLogger("skeleton_crew.test_redaction")
    logger.addHandler(handler)
    logger.propagate = False
    yield captured
    logger.removeHandler(handler)


def _logger() -> logging.Logger:
    return logging.getLogger("skeleton_crew.test_redaction")


def test_secret_in_message_is_redacted(records: list[logging.LogRecord]) -> None:
    _logger().warning(f"token={_SECRET}")
    assert records[0].getMessage() == "token=***"


def test_secret_in_args_is_redacted(records: list[logging.LogRecord]) -> None:
    _logger().warning("token=%s header=%r", _SECRET, {"auth": _SECRET})
    assert _SECRET not in records[0].getMessage()
    assert records[0].getMessage().startswith("token=***")


def test_secret_in_exception_text_is_redacted(records: list[logging.LogRecord]) -> None:
    try:
        raise RuntimeError(_SECRET)
    except RuntimeError:
        _logger().exception("failed")
    assert records[0].exc_text is not None
    assert _SECRET not in records[0].exc_text


def test_short_values_are_not_treated_as_secrets(records: list[logging.LogRecord]) -> None:
    _logger().warning("a short message")
    assert records[0].getMessage() == "a short message"


def test_redact_masks_long_secrets_and_keeps_short_values() -> None:
    assert redact(f"a {_SECRET} b short", [_SECRET, "short"]) == "a *** b short"


def test_redact_masks_a_secret_containing_another_completely() -> None:
    inner = "abcdefgh12"
    assert redact(f"x{inner}XYZ", [inner, f"{inner}XYZ"]) == "x***"
