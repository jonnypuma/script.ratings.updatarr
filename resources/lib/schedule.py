"""Decide when a scheduled run is due."""

from __future__ import annotations

import re
from datetime import datetime, timedelta


def is_due(last_run: datetime | None, hours: int, now: datetime) -> bool:
    if hours <= 0:
        return False
    if last_run is None:
        return True
    last_run, now = _same_zone(last_run, now)
    return now - last_run >= timedelta(hours=hours)


def parse_times(value: str) -> list[tuple[int, int]]:
    """Clock times such as '03:00, 15:30'. An empty or invalid value is 03:00."""
    found: list[tuple[int, int]] = []
    for part in re.split(r"[,;\s]+", str(value or "").strip()):
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", part)
        if not match:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59 and (hour, minute) not in found:
            found.append((hour, minute))
    return found or [(3, 0)]


def daily_due(last_run: datetime | None, times: list[tuple[int, int]], now: datetime) -> bool:
    """True once each configured clock time has passed since the previous run.

    The first run waits until one of today's times, instead of starting immediately.
    """
    if not times:
        return False
    if last_run is None:
        return any(now >= _at(now, hour, minute) for hour, minute in times)
    last_run, now = _same_zone(last_run, now)
    latest = max(_passed_slot(now, hour, minute) for hour, minute in times)
    return last_run < latest


def _at(now: datetime, hour: int, minute: int) -> datetime:
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _passed_slot(now: datetime, hour: int, minute: int) -> datetime:
    slot = _at(now, hour, minute)
    if now < slot:
        return slot - timedelta(days=1)
    return slot


def _same_zone(last_run: datetime, now: datetime) -> tuple[datetime, datetime]:
    if last_run.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=last_run.tzinfo)
    elif last_run.tzinfo is None and now.tzinfo is not None:
        last_run = last_run.replace(tzinfo=now.tzinfo)
    elif last_run.tzinfo is not None and now.tzinfo is not None:
        last_run = last_run.astimezone(now.tzinfo)
    return last_run, now
