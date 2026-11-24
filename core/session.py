from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")

# CME Globex sessions open at 17:00 CT and close at 16:00 CT the next day
SESSION_OPEN = time(17, 0)


def trade_date(t: datetime) -> date:
    """
    The trading date a moment belongs to, using the CME convention: a session that
    opens at 17:00 CT belongs to the next day's date. Times from Friday 17:00 CT
    through the weekend map to Monday, the next session to trade.

    Naive datetimes are treated as UTC.
    """
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    ct = t.astimezone(CHICAGO)

    d = ct.date()
    if ct.time() >= SESSION_OPEN:
        d += timedelta(days=1)

    # Saturday (5) and Sunday (6) roll forward to Monday
    if d.weekday() >= 5:
        d += timedelta(days=7 - d.weekday())

    return d
