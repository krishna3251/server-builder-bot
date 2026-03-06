"""Logging setup \u2014 rotating file handler + coloured console output."""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# \u2500\u2500 ANSI colour codes \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
_RESET = "\033[0m"
_COLOURS: dict[int, str] = {
    logging.DEBUG: "\033[37m",      # white
    logging.INFO: "\033[36m",       # cyan
    logging.WARNING: "\033[33m",    # yellow
    logging.ERROR: "\033[31m",      # red
    logging.CRITICAL: "\033[1;31m", # bold red
}


class ColouredFormatter(logging.Formatter):
    """Formatter that prepends an ANSI colour code based on log level."""

    def format(self, record: logging.LogRecord) -> str:
        colour = _COLOURS.get(record.levelno, _RESET)
        record.msg = f"{colour}{record.msg}{_RESET}"
        return super().format(record)


def setup_logging() -> None:
    """Configure root logger with console + rotating file handlers."""
    os.makedirs("logs", exist_ok=True)

    fmt = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # \u2500\u2500 Console handler (coloured) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(ColouredFormatter(fmt, datefmt=datefmt))
    root.addHandler(console)

    # \u2500\u2500 All-levels rotating file handler \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    all_file = RotatingFileHandler(
        "logs/bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    all_file.setLevel(logging.DEBUG)
    all_file.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(all_file)

    # \u2500\u2500 Error-only rotating file handler \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    err_file = RotatingFileHandler(
        "logs/error.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    err_file.setLevel(logging.ERROR)
    err_file.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(err_file)

    # Suppress noisy discord.py HTTP debug logs
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
