from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path


YELLOW = "\x1b[33m"
RED = "\x1b[31m"
RESET = "\x1b[0m"


class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if record.levelno >= logging.ERROR:
            return f"{RED}{message}{RESET}"
        if record.levelno >= logging.WARNING:
            return f"{YELLOW}{message}{RESET}"
        return message


def configure_logging(log_dir: Path, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("market_maker_tool")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(ColorFormatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    logger.addHandler(console)

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run_{datetime.now():%Y%m%d}.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(file_handler)
    return logger
