USAGE_LIMIT_REASON = "the agent usage limit was reached"


class JobStop(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Escalation(JobStop):
    pass


class Skip(JobStop):
    pass


class UsageLimitReached(Skip):
    def __init__(self) -> None:
        super().__init__(USAGE_LIMIT_REASON)


class SessionFailed(Escalation):
    pass


def error_note(exc: BaseException) -> str:
    return f"error: {type(exc).__name__}"


def error_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"
