from collections.abc import Mapping

GENERAL = ""


class NotFound(LookupError):
    pass


class InvalidInput(ValueError):
    def __init__(self, errors: Mapping[str, list[str]]) -> None:
        super().__init__(errors)
        self.errors = errors
