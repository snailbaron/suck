import logging
import sys
from typing import override

import colorama

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


def setup_log() -> None:
    formatter = LogFormatter(
        "[%(asctime)s] - %(levelname)s - %(message)s", datefmt="%Y-%M-%d %H-%M-%S %z"
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    log.addHandler(handler)
    log.setLevel(logging.INFO)
