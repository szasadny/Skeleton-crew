import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.core.audit import AuditEntry, AuditKind, AuditOutcome
from skeleton_crew.core.security import MAX_PASSWORD_CHARS, verify_password
from skeleton_crew.store import audit_store

_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 30.0


class LoginOutcome(StrEnum):
    SUCCEEDED = "login succeeded"
    FAILED = "login failed"
    THROTTLED = "login throttled"


_AUDIT_OUTCOMES = {
    LoginOutcome.SUCCEEDED: AuditOutcome.ALLOWED,
    LoginOutcome.FAILED: AuditOutcome.BLOCKED,
    LoginOutcome.THROTTLED: AuditOutcome.BLOCKED,
}


@dataclass(frozen=True)
class LoginResult:
    outcome: LoginOutcome
    retry_after_s: float = 0.0


@dataclass
class _Failures:
    count: int
    blocked_until: float


class LoginThrottle:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._failures: dict[str, _Failures] = {}

    def retry_after(self, client: str) -> float:
        entry = self._failures.get(client)
        if entry is None:
            return 0.0
        return max(0.0, entry.blocked_until - self.clock())

    def failed(self, client: str) -> None:
        entry = self._failures.get(client)
        count = 1 if entry is None else entry.count + 1
        delay = min(_BACKOFF_MAX_S, _BACKOFF_START_S * 2 ** (count - 1))
        self._failures[client] = _Failures(count, self.clock() + delay)

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)


class LoginGate:
    def __init__(self, password_hash: SecretStr, throttle: LoginThrottle | None = None) -> None:
        self._password_hash = password_hash
        self.throttle = throttle or LoginThrottle()

    async def _verify(self, password: str) -> bool:
        if len(password) > MAX_PASSWORD_CHARS:
            return False
        secret = self._password_hash.get_secret_value()
        return await asyncio.to_thread(verify_password, secret, password)

    async def attempt(self, session: AsyncSession, client: str, password: str) -> LoginResult:
        wait_s = self.throttle.retry_after(client)
        if wait_s > 0:
            result = LoginResult(LoginOutcome.THROTTLED, wait_s)
        elif await self._verify(password):
            self.throttle.succeeded(client)
            result = LoginResult(LoginOutcome.SUCCEEDED)
        else:
            self.throttle.failed(client)
            result = LoginResult(LoginOutcome.FAILED)
        entry = AuditEntry(
            AuditKind.LOGIN,
            _AUDIT_OUTCOMES[result.outcome],
            result.outcome.value,
            {"client": client},
        )
        await audit_store.record(session, entry)
        await session.commit()
        return result
