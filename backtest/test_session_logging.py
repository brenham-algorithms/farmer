import logging
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from config.logging import SessionFileHandler
from core.session import CHICAGO, trade_date


def ct(year: int, month: int, day: int, hour: int, minute: int, second: int = 0):
    return datetime(year, month, day, hour, minute, second, tzinfo=CHICAGO)


@pytest.mark.parametrize(
    "moment, expected",
    [
        # Monday session: Sunday 17:00 CT through Monday 16:00 CT
        (ct(2026, 11, 22, 17, 0), date(2026, 11, 23)),
        (ct(2026, 11, 23, 9, 30), date(2026, 11, 23)),
        (ct(2026, 11, 23, 15, 59), date(2026, 11, 23)),
        # The daily break still belongs to the session that just closed
        (ct(2026, 11, 23, 16, 30), date(2026, 11, 23)),
        (ct(2026, 11, 23, 16, 59, 59), date(2026, 11, 23)),
        # The next session opens at 17:00
        (ct(2026, 11, 23, 17, 0), date(2026, 11, 24)),
        # Friday after the close and the weekend map to Monday
        (ct(2026, 11, 27, 15, 0), date(2026, 11, 27)),
        (ct(2026, 11, 27, 17, 0), date(2026, 11, 30)),
        (ct(2026, 11, 28, 12, 0), date(2026, 11, 30)),
        (ct(2026, 11, 29, 16, 0), date(2026, 11, 30)),
        # Daylight saving: 17:00 CT is 22:00 UTC in summer, 23:00 UTC in winter
        (datetime(2026, 7, 7, 22, 0, tzinfo=timezone.utc), date(2026, 7, 8)),
        (datetime(2026, 7, 7, 21, 59, tzinfo=timezone.utc), date(2026, 7, 7)),
        (datetime(2026, 12, 7, 22, 30, tzinfo=timezone.utc), date(2026, 12, 7)),
        (datetime(2026, 12, 7, 23, 0, tzinfo=timezone.utc), date(2026, 12, 8)),
        # Naive datetimes are UTC
        (datetime(2026, 12, 7, 23, 0), date(2026, 12, 8)),
    ],
)
def test_trade_date(moment: datetime, expected: date) -> None:
    assert trade_date(moment) == expected


def _record(at: datetime, msg: str) -> logging.LogRecord:
    record = logging.LogRecord("test", logging.INFO, __file__, 0, msg, None, None)
    record.created = at.timestamp()
    return record


def test_session_file_handler_switches_at_session_open(tmp_path: Path) -> None:
    log_dir = tmp_path / "mnq_day_type"
    handler = SessionFileHandler(str(log_dir))
    try:
        handler.handle(_record(ct(2026, 11, 23, 15, 0), "before close"))
        handler.handle(_record(ct(2026, 11, 23, 16, 30), "during break"))
        handler.handle(_record(ct(2026, 11, 23, 17, 1), "next session"))
    finally:
        handler.close()

    first = (log_dir / "2026-11-23.log").read_text().splitlines()
    second = (log_dir / "2026-11-24.log").read_text().splitlines()
    assert [line.split(" ", 3)[3] for line in first] == ["before close", "during break"]
    assert [line.split(" ", 3)[3] for line in second] == ["next session"]


def test_session_file_handler_appends_after_restart(tmp_path: Path) -> None:
    for msg in ("first run", "after restart"):
        handler = SessionFileHandler(str(tmp_path))
        handler.handle(_record(ct(2026, 11, 23, 10, 0), msg))
        handler.close()

    lines = (tmp_path / "2026-11-23.log").read_text().splitlines()
    assert [line.split(" ", 3)[3] for line in lines] == ["first run", "after restart"]


def test_session_file_handler_strips_color_codes(tmp_path: Path) -> None:
    handler = SessionFileHandler(str(tmp_path))
    try:
        handler.handle(_record(ct(2026, 11, 23, 10, 0), "\x1b[32mLONG\x1b[0m entry"))
    finally:
        handler.close()

    assert (tmp_path / "2026-11-23.log").read_text().endswith("INFO LONG entry\n")
