import logging
from typing import Any

from colorama import Style, init
from pythonjsonlogger import jsonlogger


def init_null_logger(level: str) -> logging.Logger:
    """
    Logger that won't spam stdout during tests.
    """
    logger = logging.getLogger("static_bounce_test")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def init_backtest_logger(level: str) -> logging.Logger:
    """
    Logger for backtests with timestamps and colored output.
    """
    init(autoreset=True)

    logger = logging.getLogger()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # If this logger already has handlers such as pytest param runs, clear them
    logger.handlers.clear()

    handler = logging.StreamHandler()

    # Set datefmt to only show down to seconds
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

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
