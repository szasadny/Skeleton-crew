from skeleton_crew.web.pagination import PAGE_SIZE, paginate


class _Rows:
    def __init__(self, total: int) -> None:
        self.rows = list(range(total))
        self.calls: list[tuple[int, int]] = []

    async def __call__(self, limit: int, offset: int) -> list[int]:
        self.calls.append((limit, offset))
        return self.rows[offset : offset + limit]


async def test_first_page_fetches_one_extra_row_to_detect_a_next_page() -> None:
    rows = _Rows(PAGE_SIZE + 1)
    page = await paginate(rows, 1)
    assert rows.calls == [(PAGE_SIZE + 1, 0)]
    assert list(page.items) == list(range(PAGE_SIZE))
    assert (page.number, page.has_next) == (1, True)


async def test_last_page_has_no_next_page() -> None:
    page = await paginate(_Rows(7), 2, size=5)
    assert list(page.items) == [5, 6]
    assert page.has_next is False


async def test_exactly_full_page_has_no_next_page() -> None:
    page = await paginate(_Rows(5), 1, size=5)
    assert len(page.items) == 5
    assert page.has_next is False


async def test_page_past_the_end_is_empty() -> None:
    page = await paginate(_Rows(3), 9, size=5)
    assert list(page.items) == []
    assert page.has_next is False
