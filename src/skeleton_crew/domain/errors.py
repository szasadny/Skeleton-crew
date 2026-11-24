from dataclasses import dataclass


class PolicyViolation(Exception):
    pass


class InvalidTransition(Exception):
    pass


@dataclass(frozen=True)
class Violation:
    path: str
    line: int | None
    reason: str

    def __str__(self) -> str:
        where = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"{where}: {self.reason}"
