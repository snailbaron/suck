import argparse
import asyncio
import dataclasses
import hashlib
import logging
import shutil
import sys
import urllib.parse
from collections.abc import Coroutine, Sequence
from pathlib import Path

import aiohttp
import pydantic
import rich.progress
import yaml

from . import input_model, util
from .log import log, setup_log

CHUNK_SIZE_BYTES = 1 << 20


@dataclasses.dataclass(frozen=True)
class FileInfo:
    url: str
    checksum_type: str
    checksum: str | None

    def name(self) -> str:
        return urllib.parse.urlparse(self.url).path.rpartition("/")[-1]


def get_file_checksum(file_path: Path, checksum_type: str) -> str:
    file_hash = hashlib.new(checksum_type)

    with file_path.open("rb") as f:
        while chunk := f.read(CHUNK_SIZE_BYTES):
            file_hash.update(chunk)

    return file_hash.hexdigest()


def check_existing_file_hashes(
    files_info: Sequence[FileInfo], output_path: Path
) -> list[FileInfo]:
    log.info("checking existing files")

    checksum_match_count = 0
    files_to_download: list[FileInfo] = []
    for fi in files_info:
        file_path = output_path / fi.name()

        if file_path.exists() and fi.checksum:
            file_real_checksum = get_file_checksum(file_path, fi.checksum_type)
            if file_real_checksum == fi.checksum:
                checksum_match_count += 1
            else:
                log.warning("%s: checksum mismatch", file_path)
                files_to_download.append(fi)
        else:
            files_to_download.append(fi)

    if checksum_match_count > 0:
        log.info(
            "%d files to download (%d already downloaded)",
            len(files_to_download),
            checksum_match_count,
        )
    else:
        log.info("%d files to download", len(files_to_download))

    return files_to_download


async def download_file(
    *,
    progress: rich.progress.Progress,
    session: aiohttp.ClientSession,
    file_info: FileInfo,
    output_path: Path,
) -> None:
    task_id = progress.add_task(description=f"{file_info.name()}", start=False)

    file_name = file_info.name()
    file_path = output_path / file_name
    downloaded_file_hash = hashlib.new(file_info.checksum_type)
    try:
        async with session.get(file_info.url) as r:
            file_size: int | None = None
            if content_length := r.headers.get("Content-Length"):
                file_size = int(content_length)

            progress.start_task(task_id)
            progress.update(task_id, total=file_size)

            with file_path.open("wb") as f:
                async for chunk in r.content.iter_chunked(CHUNK_SIZE_BYTES):
                    downloaded_file_hash.update(chunk)
                    f.write(chunk)
                    progress.update(task_id, advance=len(chunk))

    except aiohttp.ClientError as e:
        message = f"{file_name}: {e}"
        if len(message) > 50:
            message = message[:47] + "..."
        progress.update(task_id, description=message)
        return

    if file_info.checksum and downloaded_file_hash.hexdigest() != file_info.checksum:
        progress.update(task_id, description=f"{file_name}: checksum mismatch")


async def process_files(
    info: input_model.Input, output_path: Path, *, max_parallel_downloads: int
) -> None:
    files_info: list[FileInfo] = [
        FileInfo(
            url=fi.url,
            checksum_type=fi.checksum_type or info.default_checksum_type,
            checksum=fi.checksum,
        )
        for fi in info.files
    ]

    files_to_download = check_existing_file_hashes(files_info, output_path)

    with rich.progress.Progress() as progress:
        async with aiohttp.ClientSession() as session:
            tasks: list[Coroutine[None, None, None]] = []

            for file_info in files_to_download:
                tasks.append(
                    download_file(
                        progress=progress,
                        session=session,
                        file_info=file_info,
                        output_path=output_path,
                    )
                )

            await util.gather_with_limit(max_parallel_downloads, *tasks)


def dump_example_input(output_path: Path) -> None:
    example_input = input_model.Input(
        files=[
            input_model.FileInput(
                url="https://example-url-1", checksum="0123456789abcdef"
            ),
            input_model.FileInput(
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

    input_data = None
    try:
        with util.FileOrStdin(input_path) as f:
            input_data = input_model.Input.model_validate(yaml.safe_load(f))
    except pydantic.ValidationError as e:
        for error in e.errors():
            error_path = "->".join(str(x) for x in error["loc"])
            print(f"[{error['type']}] {error_path}: {error['msg']}", file=sys.stderr)
        return 1

    assert input_data

    if clean and output_path.exists():
        shutil.rmtree(output_path)

    output_path.mkdir(parents=True, exist_ok=True)

    try:
        asyncio.run(
            process_files(
                input_data, output_path, max_parallel_downloads=max_parallel_downloads
            )
        )
    except KeyboardInterrupt:
        log.warning("Interrupted")

    return 0
