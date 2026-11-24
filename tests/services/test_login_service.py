import pytest
from audit_helpers import audit_events
from clocks import FakeClock
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.core.security import MAX_PASSWORD_CHARS, hash_password
from skeleton_crew.services.login_service import (
    LoginGate,
    LoginOutcome,
    LoginResult,
    LoginThrottle,
)

_PASSWORD = "correct horse battery staple"  # noqa: S105
_HASH = SecretStr(hash_password(_PASSWORD))
_CLIENT = "10.0.0.1"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gate(clock: FakeClock) -> LoginGate:
    return LoginGate(_HASH, LoginThrottle(clock.monotonic))


async def test_right_password_succeeds_and_is_audited(
    session: AsyncSession, gate: LoginGate
) -> None:
    assert await gate.attempt(session, _CLIENT, _PASSWORD) == LoginResult(LoginOutcome.SUCCEEDED)
    events = await audit_events(session, AuditKind.LOGIN)
    assert [(e.outcome, e.summary, e.payload) for e in events] == [
        (AuditOutcome.ALLOWED, "login succeeded", {"client": _CLIENT})
    ]


async def test_wrong_password_fails_and_the_attempt_is_audited_without_it(
    session: AsyncSession, gate: LoginGate
) -> None:
    result = await gate.attempt(session, _CLIENT, "guess-123456")
    assert result.outcome is LoginOutcome.FAILED
    events = await audit_events(session, AuditKind.LOGIN)
    assert [(e.outcome, e.summary) for e in events] == [(AuditOutcome.BLOCKED, "login failed")]
    assert "guess-123456" not in repr([(e.summary, e.payload) for e in events])


async def test_right_password_is_refused_while_throttled(
    session: AsyncSession, gate: LoginGate, clock: FakeClock
) -> None:
    await gate.attempt(session, _CLIENT, "nope")
    clock.advance(0.4)
    result = await gate.attempt(session, _CLIENT, _PASSWORD)
    assert result.outcome is LoginOutcome.THROTTLED
    assert result.retry_after_s == pytest.approx(0.6)
    clock.advance(0.7)
    assert (await gate.attempt(session, _CLIENT, _PASSWORD)).outcome is LoginOutcome.SUCCEEDED
    summaries = [e.summary for e in await audit_events(session, AuditKind.LOGIN)]
    assert summaries == ["login failed", "login throttled", "login succeeded"]


async def test_throttle_is_per_client(session: AsyncSession, gate: LoginGate) -> None:
    await gate.attempt(session, _CLIENT, "nope")
    other = await gate.attempt(session, "10.0.0.2", _PASSWORD)
    assert other.outcome is LoginOutcome.SUCCEEDED


async def test_overlong_password_fails_even_when_it_matches_the_hash(
    session: AsyncSession, clock: FakeClock
) -> None:
    long_password = "x" * (MAX_PASSWORD_CHARS + 1)
    gate = LoginGate(SecretStr(hash_password(long_password)), LoginThrottle(clock.monotonic))
    assert (await gate.attempt(session, _CLIENT, long_password)).outcome is LoginOutcome.FAILED


async def test_empty_password_fails(session: AsyncSession, gate: LoginGate) -> None:
    assert (await gate.attempt(session, _CLIENT, "")).outcome is LoginOutcome.FAILED


def test_backoff_doubles_up_to_thirty_seconds(clock: FakeClock) -> None:
    throttle = LoginThrottle(clock.monotonic)
    waits = []
    for _ in range(7):
        throttle.failed(_CLIENT)
        waits.append(throttle.retry_after(_CLIENT))
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_success_clears_the_backoff(clock: FakeClock) -> None:
    throttle = LoginThrottle(clock.monotonic)
    throttle.failed(_CLIENT)
    throttle.succeeded(_CLIENT)
    assert throttle.retry_after(_CLIENT) == 0.0
    throttle.failed(_CLIENT)
    assert throttle.retry_after(_CLIENT) == 1.0
