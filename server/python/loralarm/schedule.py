"""Daily time windows ("21:00-09:00") in a named time zone.

Used by the alarm window: notifications for windowed alarm kinds are only sent
while the local time is inside the window. Stdlib only (`zoneinfo`, so summer
time is handled); on a system without time zone data (slim containers) install
the `tzdata` package — an unknown zone is reported as a ValueError, and the
caller treats that as "no window".

    parse_window("21:00-09:00").contains(local_time(ts, "Europe/Stockholm"))

Start is inclusive and end exclusive. A window whose start is later than its
end runs overnight. `start == end` is rejected as ambiguous (empty or all day?).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


@dataclass(frozen=True)
class Window:
    start: time
    end: time

    def contains(self, t: time) -> bool:
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end      # overnight

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def parse_window(text: str) -> Window:
    """"HH:MM-HH:MM" -> Window; ValueError for anything else."""
    m = _WINDOW_RE.match(text or "")
    if not m:
        raise ValueError(f"alarm_window {text!r}: expected 'HH:MM-HH:MM', e.g. '21:00-09:00'")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError(f"alarm_window {text!r}: hours are 00-23 and minutes 00-59")
    start, end = time(h1, m1), time(h2, m2)
    if start == end:
        raise ValueError(f"alarm_window {text!r}: start and end are equal; "
                         "leave alarm_window empty for 'always'")
    return Window(start, end)


@lru_cache(maxsize=32)
def zone(name: str) -> ZoneInfo:
    """ZoneInfo by name; ValueError if the zone is unknown or has no data."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        raise ValueError(f"unknown time zone {name!r} (is the tzdata package installed?)") from e


def local_time(ts: datetime, tzname: str) -> time:
    """Wall-clock time of `ts` in the zone; a naive `ts` is taken as UTC."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(zone(tzname)).time()
