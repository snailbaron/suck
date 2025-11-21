import argparse
import asyncio
import dataclasses
import hashlib
import logging
import shutil
import sys
import urllib.parse
from collections.abc import AsyncGenerator, Coroutine
from pathlib import Path
from typing import Any, override

import aiofiles
import aiofiles.os
import aiohttp
import colorama
import pydantic
import rich.progress
import yaml

CHUNK_SIZE_BYTES = 1 << 20

log = logging.getLogger("suck")


class LogFormatter(logging.Formatter):
    @override
    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        end = colorama.Style.RESET_ALL

        match record.levelno:
            case logging.DEBUG:
                return colorama.Style.DIM + colorama.Fore.WHITE + s + end
            case logging.WARNING:
                return colorama.Fore.YELLOW + s + end
            case logging.ERROR:
                return colorama.Fore.RED + s + end
            case _:
                return s


def setup_log():
    formatter = LogFormatter(
        "[%(asctime)s] - %(levelname)s - %(message)s", datefmt="%Y-%M-%d %H-%M-%S %z"
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    log.addHandler(handler)
    log.setLevel(logging.INFO)


class Model(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")


class FileInput(Model):
    url: str
    checksum: str | None = None
    checksum_type: str | None = None


class Input(Model):
    default_checksum_type: str = "md5"
    files: list[FileInput]


@dataclasses.dataclass
class ProgressInfo:
    progress: rich.progress.Progress
    task_id: rich.progress.TaskID

    def start_task(self) -> None:
        self.progress.start_task(self.task_id)

    def stop_task(self) -> None:
        self.progress.stop_task(self.task_id)

    def update(
        self,
        total: float | None = None,
        completed: float | None = None,
        advance: float | None = None,
        description: str | None = None,
        visible: bool | None = None,
        refresh: bool = False,
        **fields: Any,
    ) -> None:
        self.progress.update(
            self.task_id,
            total=total,
            completed=completed,
            advance=advance,
            description=description,
            visible=visible,
            refresh=refresh,
            **fields,
        )


async def check_file_hash(
    pr: ProgressInfo, *, file_path: Path, checksum_type: str, checksum: str
) -> bool:
    file_hash = hashlib.new(checksum_type)
    file_name = file_path.name

    existing_file_size = (await aiofiles.os.stat(file_path)).st_size
    pr.start_task()
    pr.update(
        total=existing_file_size, completed=0, description=f"{file_name}: checking hash"
    )

    async with aiofiles.open(file_path, "rb") as f:
        while chunk := await f.read(CHUNK_SIZE_BYTES):
            file_hash.update(chunk)
            pr.update(advance=len(chunk))

    if file_hash.hexdigest() == checksum:
        pr.update(description=f"{file_name}: in place")
        return True
    else:
        pr.update(description=f"{file_name}: mismatch")
        return False


async def download_file(
    pr: ProgressInfo,
    *,
    file_path: Path,
    checksum_type: str,
    checksum: str | None,
    session: aiohttp.ClientSession,
    url: str,
) -> None:
    file_name = file_path.name

    pr.update(description=f"{file_name}: requesting")

    downloaded_file_hash = hashlib.new(checksum_type)
    try:
        log.debug("trying a GET request")
        async with aiofiles.open(file_path, "wb") as f, session.get(url) as r:
            log.debug("got GET response awaitable")
            file_size: int | None = None
            if content_length := r.headers.get("Content-Length"):
                file_size = int(content_length)

            pr.update(
                description=f"{file_name}: downloading", total=file_size, completed=0
            )
            pr.start_task()

            async for chunk in r.content.iter_chunked(CHUNK_SIZE_BYTES):
                downloaded_file_hash.update(chunk)
                await f.write(chunk)
                pr.update(advance=len(chunk))

    except aiohttp.ClientError as e:
        message = f"{file_name}: {e}"
        if len(message) > 50:
            message = message[:47] + "..."
        pr.update(description=message)
        pr.stop_task()
        return

    if checksum and downloaded_file_hash.hexdigest() != checksum:
        pr.update(description=f"{file_name}: bad checksum")
    else:
        pr.update(description=f"{file_name}: downloaded")


async def process_file(
    *,
    progress: rich.progress.Progress,
    session: aiohttp.ClientSession,
    url: str,
    checksum_type: str,
    checksum: str | None,
    output_path: Path,
) -> None:
    file_name = urllib.parse.urlparse(url).path.rpartition("/")[-1]
    file_path = output_path / file_name

    progress_id = progress.add_task(f"{file_name}", start=False)
    pr = ProgressInfo(progress, progress_id)

    log.debug("checking if file exists")
    log.debug("file path: %s", file_path.absolute())
    log.debug("file exists: %s", file_path.exists())
    log.debug("checksum: %s", checksum)
    log.debug("pwd: %s", Path.cwd())
    if file_path.exists() and checksum:
        log.debug("file exists")
        if await check_file_hash(
            pr, file_path=file_path, checksum_type=checksum_type, checksum=checksum
        ):
            log.debug("hash matches")
            return
        else:
            log.debug("hash does not match")
    else:
        log.debug("file does not exist or checksum not set")

    await download_file(
        pr,
        file_path=file_path,
        checksum_type=checksum_type,
        checksum=checksum,
        session=session,
        url=url,
    )


async def gather_with_limit(limit: int, *coros: Coroutine[None, None, None]) -> None:
    semaphore = asyncio.Semaphore(limit)

    async def wrapper(coro: Coroutine[None, None, None]) -> None:
        async with semaphore:
            return await coro

    await asyncio.gather(*(wrapper(coro) for coro in coros))


async def process_files(
    info: Input, output_path: Path, *, max_parallel_downloads: int
) -> None:
    with rich.progress.Progress() as progress:
        async with aiohttp.ClientSession() as session:
            tasks: list[Coroutine[None, None, None]] = []

            for file in info.files:
                checksum_type = file.checksum_type or info.default_checksum_type
                tasks.append(
                    process_file(
                        progress=progress,
                        session=session,
                        url=file.url,
                        checksum_type=checksum_type,
                        checksum=file.checksum,
                        output_path=output_path,
                    )
                )

            await gather_with_limit(max_parallel_downloads, *tasks)


def dump_example_input(output_path: Path) -> None:
    example_input = Input(
        files=[
            FileInput(url="https://example-url-1", checksum="0123456789abcdef"),
            FileInput(
                url="https://example-url-2",
                checksum_type="sha256",
                checksum="0123456789abcdef",
            ),
        ],
    )

    with output_path.open("w") as f:
        yaml.safe_dump(example_input.model_dump(exclude_none=True), f)


def main() -> int | None:
    setup_log()

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-i", "--input", metavar="FILE", type=Path, help="path to the input file"
    )
    parser.add_argument(
        "-o", "--output", metavar="DIR", type=Path, help="path to the output directory"
    )
    parser.add_argument(
        "--clean", action="store_true", help="download to a clean directory"
    )
    parser.add_argument(
        "--dump-example-input",
        metavar="FILE",
        type=Path,
        help="dump example input file",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="print more output"
    )
    parser.add_argument(
        "--max-parallel-downloads",
        metavar="N",
        type=int,
        default=10,
        help="max number of parallel downloads",
    )
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    input_path: Path | None = args.input
    output_path: Path = args.output or Path.cwd()
    example_path: Path | None = args.dump_example_input
    clean: bool = args.clean
    max_parallel_downloads: int = args.max_parallel_downloads

    if example_path:
        dump_example_input(example_path)
        return 0

    try:
        if input_path:
            with input_path.open() as f:
                info = Input.model_validate(yaml.safe_load(f))
        else:
            info = Input.model_validate(yaml.safe_load(sys.stdin))
    except pydantic.ValidationError as e:
        for error in e.errors():
            error_path = "->".join(str(x) for x in error["loc"])
            print(f"[{error['type']}] {error_path}: {error['msg']}", file=sys.stderr)
        return 1

    if clean and output_path.exists():
        shutil.rmtree(output_path)

    output_path.mkdir(parents=True, exist_ok=True)
    asyncio.run(
        process_files(info, output_path, max_parallel_downloads=max_parallel_downloads)
    )

    return 0
