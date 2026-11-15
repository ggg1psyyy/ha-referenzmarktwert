"""When to download what. Pure: no Home Assistant, no network, no clock.

- Generation: only in the daylight window (sunrise + offset .. sunset + offset), every `interval`;
  once more after the window ended if nothing was fetched since; one full download per night.
- Prices: day-ahead, published once a day. Fetched when today's are missing, and from `price_time`
  on until tomorrow's are there (every `price_retry`).
- Reference month (same month last year): whenever it is missing; no own timer.
- E-Control: daily; more often until `official_early_days` while last month's value is awaited;
  every `interval` while the download fails.

`plan()` returns the due tasks and when the next one becomes due, so the coordinator wakes up
exactly then (also at night, when only prices, E-Control or the month change may be due).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from enum import StrEnum

MIN_WAIT = 60.0  # never wake up more often than this (seconds)
MAX_WAIT = 86400.0


class Task(StrEnum):
    FULL = "full"  # generation, the whole running month (+ previous month until it is official)
    GENERATION = "generation"  # generation since the last stored point (minus the overlap)
    PRICE = "price"
    REFERENCE = "reference"
    OFFICIAL = "official"


ENERGY_CHARTS_TASKS = frozenset({Task.FULL, Task.GENERATION, Task.PRICE, Task.REFERENCE})

# sunrise and sunset (aware datetimes) for a local date, or None if the sun does not rise or set
type SunTimes = Callable[[date], tuple[datetime, datetime] | None]


@dataclass(frozen=True, slots=True)
class Settings:
    """Durations in seconds, times of day in the configured time zone."""

    interval: float
    sunrise_offset: float
    sunset_offset: float
    full_refresh_time: time
    price_time: time
    price_retry: float
    official_interval: float
    official_early_interval: float
    official_early_days: int


@dataclass(frozen=True, slots=True)
class Status:
    """What the coordinator knows (Unix seconds; None = never)."""

    generation_attempt: float | None
    full_refresh: float | None
    price_attempt: float | None
    prices_until: float | None  # end of the last stored price
    reference_missing: bool
    official_attempt: float | None
    official_failing: bool
    official_awaited: bool  # last month's official value is not known yet
    not_before: float | None  # Energy-Charts rate limit


@dataclass(frozen=True, slots=True)
class Plan:
    tasks: frozenset[Task]
    next_run: float  # Unix seconds


def _at(day: date, clock: time, tz: tzinfo) -> float:
    return datetime.combine(day, clock, tz).timestamp()


def _windows(day: date, sun: SunTimes, s: Settings) -> tuple[float, float] | None:
    times = sun(day)
    if times is None:
        return None
    sunrise, sunset = times
    return sunrise.timestamp() + s.sunrise_offset, sunset.timestamp() + s.sunset_offset


@dataclass(frozen=True, slots=True)
class Daylight:
    inside: bool  # inside a window now
    last_end: float | None  # end of the latest window that has ended
    next_start: float | None  # start of the next window
    next_end: float | None  # end of the next window that has not ended yet


def daylight(now: float, tz: tzinfo, sun: SunTimes, s: Settings) -> Daylight:
    """Where `now` is relative to the daylight windows.

    Yesterday's window is included: sunset + offset can be after midnight. Without sun times
    (polar day or night) there is no gating: always inside.
    """
    today = datetime.fromtimestamp(now, tz).date()
    found = [_windows(today + timedelta(days=d), sun, s) for d in (-1, 0, 1)]
    windows = [w for w in found if w is not None]
    if len(windows) < len(found):
        return Daylight(True, None, None, None)
    return Daylight(
        inside=any(start <= now < end for start, end in windows),
        last_end=max((end for _, end in windows if end <= now), default=None),
        next_start=min((start for start, _ in windows if start > now), default=None),
        next_end=min((end for _, end in windows if end > now), default=None),
    )


def _last_occurrence(now: float, clock: time, tz: tzinfo) -> tuple[float, date]:
    """The latest time `clock` (local) at or before `now`, and its date."""
    today = datetime.fromtimestamp(now, tz).date()
    day = today if _at(today, clock, tz) <= now else today - timedelta(days=1)
    return _at(day, clock, tz), day


def _elapsed(since: float | None, now: float, wait: float) -> bool:
    return since is None or now - since >= wait


def plan(now: float, tz: tzinfo, sun: SunTimes, s: Settings, st: Status) -> Plan:
    today = datetime.fromtimestamp(now, tz).date()
    end_of_today = _at(today + timedelta(days=1), time(), tz)
    end_of_tomorrow = _at(today + timedelta(days=2), time(), tz)
    price_today = _at(today, s.price_time, tz)
    day = daylight(now, tz, sun, s)
    last_full_slot, last_full_day = _last_occurrence(now, s.full_refresh_time, tz)

    tasks: set[Task] = set()
    wake: list[float] = [_at(last_full_day + timedelta(days=1), s.full_refresh_time, tz)]

    # --- generation
    if st.full_refresh is None or st.full_refresh < last_full_slot:
        tasks.add(Task.FULL)
    elif day.inside:
        if _elapsed(st.generation_attempt, now, s.interval):
            tasks.add(Task.GENERATION)
    elif day.last_end is not None and (st.generation_attempt is None or st.generation_attempt < day.last_end):
        tasks.add(Task.GENERATION)  # once more after the window: the last daylight hours
    if day.inside:
        wake.append((st.generation_attempt or now) + s.interval)
    wake += [w for w in (day.next_start, day.next_end) if w is not None]

    # --- prices
    have = st.prices_until or 0.0
    if have < end_of_today:
        if _elapsed(st.price_attempt, now, s.price_retry):
            tasks.add(Task.PRICE)
        wake.append((st.price_attempt or now) + s.price_retry)
    elif have < end_of_tomorrow:
        if now >= price_today and _elapsed(st.price_attempt, now, s.price_retry):
            tasks.add(Task.PRICE)
        wake.append(max(price_today, (st.price_attempt or now) + s.price_retry))
    else:
        wake.append(_at(today + timedelta(days=1), s.price_time, tz))

    if st.reference_missing:
        tasks.add(Task.REFERENCE)

    # --- E-Control
    if st.official_failing:
        wait = s.interval
    elif st.official_awaited and today.day <= s.official_early_days:
        wait = s.official_early_interval
    else:
        wait = s.official_interval
    if _elapsed(st.official_attempt, now, wait):
        tasks.add(Task.OFFICIAL)
    wake.append((st.official_attempt or now) + wait)

    # --- month change: the estimate switches to the new month even if nothing is downloaded
    first = today.replace(day=1)
    wake.append(_at((first + timedelta(days=32)).replace(day=1), time(), tz))

    # --- Energy-Charts asked us to wait
    if st.not_before is not None and now < st.not_before:
        tasks -= ENERGY_CHARTS_TASKS
        wake.append(st.not_before)

    future = [w for w in wake if w > now]
    next_run = min(future, default=now + MAX_WAIT)
    next_run = min(max(next_run, now + MIN_WAIT), now + MAX_WAIT)
    return Plan(frozenset(tasks), next_run)
