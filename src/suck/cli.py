import argparse
import asyncio
import hashlib
import logging
import shutil
import sys
import urllib.parse
from collections.abc import Coroutine
from pathlib import Path
from typing import override

import aiofiles
import aiohttp
import colorama
import pydantic
import yaml
from tqdm.auto import tqdm

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


async def get_file_hash(
    progress_bar_index: int, checksum_type: str, file_path: Path
) -> str:
    file_hash = hashlib.new(checksum_type)
    file_name = file_path.name

    with tqdm(
        desc=f"{file_name}: check",
        unit="B",
        unit_scale=True,
        total=file_path.stat().st_size,
        position=progress_bar_index,
        leave=False,
    ) as progress:
        async with aiofiles.open(file_path, "rb") as f:
            while chunk := await f.read(CHUNK_SIZE_BYTES):
                file_hash.update(chunk)
                progress.update(len(chunk))

    return file_hash.hexdigest()


async def download_file(
    *,
    index: int,
    session: aiohttp.ClientSession,
    url: str,
    checksum_type: str,
    checksum: str | None,
    output_path: Path,
) -> None:
    hash_value = hashlib.new(checksum_type)
    file_name = urllib.parse.urlparse(url).path.rpartition("/")[-1]
    file_path = output_path / file_name

    if file_path.exists() and checksum:
        if await get_file_hash(index, checksum_type, file_path) == checksum:
            return

    log.debug("downloading file: %s", url)
    async with aiofiles.open(file_path, "wb") as f, session.get(url) as r:
        file_size: int | None = None
        if content_length := r.headers.get("Content-Length"):
            file_size = int(content_length)

        with tqdm(
            desc=f"{file_name}: download",
            unit="B",
            unit_scale=True,
            total=file_size,
            position=index,
            leave=False,
        ) as progress:
            async for chunk in r.content.iter_chunked(CHUNK_SIZE_BYTES):
                hash_value.update(chunk)
                await f.write(chunk)
                progress.update(len(chunk))

    if checksum and hash_value.hexdigest() != checksum:
        raise RuntimeError("hash does not match")


async def download_files(info: Input, output_path: Path) -> None:
    async with aiohttp.ClientSession() as session:
        tasks: list[Coroutine[None, None, None]] = []

        for i, file in enumerate(info.files):
            checksum_type = file.checksum_type or info.default_checksum_type
            tasks.append(
                download_file(
                    index=i,
                    session=session,
                    url=file.url,
                    checksum_type=checksum_type,
                    checksum=file.checksum,
                    output_path=output_path,
                )
            )

        await asyncio.gather(*tasks)


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
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    input_path: Path | None = args.input
    output_path: Path = args.output or Path.cwd()
    example_path: Path | None = args.dump_example_input
    clean: bool = args.clean

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
    asyncio.run(download_files(info, output_path))

    return 0
