import asyncio
import sys
from collections.abc import Coroutine
from pathlib import Path
from types import TracebackType
from typing import TextIO


class FileOrStdin:
    def __init__(self, path_or_none: Path | None) -> None:
        self._path = path_or_none
        self._file: TextIO | None = None

    def __enter__(self) -> TextIO:
        if self._path:
            self._file = self._path.open()
            return self._file

        return sys.stdin

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file:
            self._file.close()


async def gather_with_limit(limit: int, *coros: Coroutine[None, None, None]) -> None:
    semaphore = asyncio.Semaphore(limit)

    async def wrapper(coro: Coroutine[None, None, None]) -> None:
        async with semaphore:
            return await coro

    await asyncio.gather(*(wrapper(coro) for coro in coros))
