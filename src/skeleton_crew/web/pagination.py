from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated

from fastapi import Query

MAX_PAGE = 100_000
PAGE_SIZE = 25

PageParam = Annotated[int, Query(ge=1, le=MAX_PAGE)]


@dataclass(frozen=True)
class Page[T]:
    items: Sequence[T]
    number: int
    has_next: bool


async def paginate[T](
    fetch: Callable[[int, int], Awaitable[Sequence[T]]], page: int, size: int = PAGE_SIZE
) -> Page[T]:
    rows = await fetch(size + 1, (page - 1) * size)
    return Page(rows[:size], page, len(rows) > size)
