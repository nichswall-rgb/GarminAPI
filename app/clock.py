from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import settings


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def local_today() -> str:
    """Today's calendar date in settings.timezone, as YYYY-MM-DD.

    Deliberately not date.today(): the container clock is UTC and the image has
    no OS timezone files, so TZ is ignored there and date.today() rolls over
    mid-afternoon Pacific. The poller then asks Garmin for a day that has not
    happened yet and stores empty stubs. ZoneInfo reads the tzdata package
    pinned in requirements, which is the same path APScheduler already uses.
    """
    return _now().date().isoformat()


def local_day_offset(days_back: int) -> str:
    """The date `days_back` days before today, in settings.timezone."""
    return (_now().date() - timedelta(days=days_back)).isoformat()
