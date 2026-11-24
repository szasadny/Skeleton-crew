from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web_helpers import Web, WebFactory, open_web


@pytest.fixture
def web_factory(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> WebFactory:
    def factory(*, login: bool = True, **options: Any) -> AbstractAsyncContextManager[Web]:
        return open_web(tmp_path, sessionmaker, login=login, **options)

    return factory


@pytest.fixture
async def anon_web(web_factory: WebFactory) -> AsyncIterator[Web]:
    async with web_factory(login=False) as harness:
        yield harness


@pytest.fixture
async def web(web_factory: WebFactory) -> AsyncIterator[Web]:
    async with web_factory() as harness:
        yield harness
