import logging
import os
import re
from datetime import date, datetime, timezone
from typing import IO, Any, Optional

from colorama import Style, init
from pythonjsonlogger import jsonlogger

from core.session import trade_date

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Color codes added by log_with_color: fine on a terminal, noise in a file
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class _PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _ANSI.sub("", super().format(record))


class SessionFileHandler(logging.Handler):
    """
    Writes each trading session to its own file, {log_dir}/{YYYY-MM-DD}.log, named by
    trade date (core.session.trade_date): the file switches at 17:00 CT, during the
    daily break, so one file holds one whole session. Restarting during a session
    appends to that session's file.

    The date comes from each record's wall-clock time, so this is for live runs.
    """

    def __init__(self, log_dir: str) -> None:
        super().__init__()
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self._date: Optional[date] = None
        self._stream: Optional[IO[str]] = None
        self.setFormatter(_PlainFormatter(LOG_FORMAT, datefmt=LOG_DATEFMT))

    def path_for(self, d: date) -> str:
        return os.path.join(self.log_dir, f"{d.isoformat()}.log")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            d = trade_date(datetime.fromtimestamp(record.created, tz=timezone.utc))
            if d != self._date or self._stream is None:
                self._close_stream()
                self._stream = open(self.path_for(d), "a", encoding="utf-8")
                self._date = d
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def close(self) -> None:
        self.acquire()
        try:
            self._close_stream()
        finally:
            self.release()
        super().close()


def init_null_logger(level: str) -> logging.Logger:
    """
    Logger that won't spam stdout during tests.
    """
    logger = logging.getLogger("static_bounce_test")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def init_backtest_logger(level: str, log_dir: Optional[str] = None) -> logging.Logger:
    """
    Logger with timestamps and colored output on stdout. With log_dir, it also writes
    one file per trading session there (SessionFileHandler).
    """
    init(autoreset=True)

    logger = logging.getLogger()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # If this logger already has handlers such as pytest param runs, clear them
    logger.handlers.clear()

    handler = logging.StreamHandler()

    # Set datefmt to only show down to seconds
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    if log_dir:
        logger.addHandler(SessionFileHandler(log_dir))

    return logger


def init_strucutred_logger(level: str) -> logging.Logger:
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


def log_with_color(logger: Any, message: str, color: str, level: str) -> None:
    if level == "debug":
        logger.debug(f"{color}{message}{Style.RESET_ALL}")
    elif level == "warning":
        logger.warning(f"{color}{message}{Style.RESET_ALL}")
    elif level == "error":
        logger.error(f"{color}{message}{Style.RESET_ALL}")
    else:
        logger.info(f"{color}{message}{Style.RESET_ALL}")
