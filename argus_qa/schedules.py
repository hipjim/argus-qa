"""When a schedule is next due: chosen weekdays at a wall-clock time in a timezone."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def zone(name: str) -> tzinfo:
    """The named timezone. Raises ValueError for an unknown name."""
    if name.upper() == "UTC":
        return UTC  # works without a timezone database
    try:
        return ZoneInfo(name)
    except (KeyError, OSError, ValueError) as e:  # ZoneInfoNotFoundError is a KeyError
        raise ValueError(f"Unknown timezone {name!r}; use an IANA name like 'Europe/Bucharest'.") from e


def next_fire(days: list[str], at: str, timezone: str, after: datetime) -> datetime:
    """The first moment after `after` that falls on one of `days` at `at` (HH:MM) in `timezone`.

    No days means every day. Returns UTC. On the day clocks go forward, a time
    inside the skipped hour fires an hour later; when they go back, it fires once.
    """
    tz = zone(timezone)
    hour, minute = (int(part) for part in at.split(":"))
    wanted = {DAYS.index(d) for d in days} or set(range(7))
    start = after.astimezone(tz).date()
    for offset in range(8):
        day = start + timedelta(days=offset)
        if day.weekday() not in wanted:
            continue
        due = datetime.combine(day, time(hour, minute), tzinfo=tz).astimezone(UTC)
        if due > after:
            return due
    raise AssertionError("no due time within a week")  # unreachable: `wanted` is never empty
