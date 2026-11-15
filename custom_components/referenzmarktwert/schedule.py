"""When to download what. Pure: no Home Assistant, no network, no clock.

- Generation: only in the production window, every `interval`; once more after the window ended
  if nothing was fetched since; one full download per night. The window is taken from the data
  itself: the first and the last value > 0 of the latest stored day before today (normally
  yesterday), moved to today, plus `start_offset` and `end_offset`. Nothing about the home location
  is used, so the request times reveal nothing about it.
- Prices: day-ahead, published once a day. Fetched when today's are missing, and from `price_time`
  on until tomorrow's are there (every `price_retry`).
- Reference month (same month last year): whenever it is missing; no own timer.
- E-Control: daily; more often until `official_early_days` while last month's value is awaited;
  every `interval` while the download fails.

`plan()` returns the due tasks and when the next one becomes due, so the coordinator wakes up
exactly then (also at night, when only prices, E-Control or the month change may be due).
"""

from collections.abc import Iterable
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

DAY = 86400

# the first and the last point with generation > 0 of one day (Unix seconds)
type Production = tuple[int, int]


@dataclass(frozen=True, slots=True)
class Settings:
    """Durations in seconds, times of day in the configured time zone."""

    interval: float
    start_offset: float  # the window starts this long after the first generation
    end_offset: float  # ... and ends this long after the last (Energy-Charts publishes ~2 h late)
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
    production: Production | None  # see `production_hours`; None: no gating


@dataclass(frozen=True, slots=True)
class Plan:
    tasks: frozenset[Task]
    next_run: float  # Unix seconds


def _at(day: date, clock: time, tz: tzinfo) -> float:
    return datetime.combine(day, clock, tz).timestamp()


def production_hours(series: Iterable[tuple[int, float | None]], tz: tzinfo, today: date) -> Production | None:
    """First and last point with generation > 0 of the latest day before `today` that has any.

    Normally that is yesterday; an older day only if yesterday is missing (e.g. an outage).
    """
    days: dict[date, list[int]] = {}
    for ts, value in series:
        if value is not None and value > 0:
            day = datetime.fromtimestamp(ts, tz).date()
            if day < today:
                days.setdefault(day, []).append(ts)
    if not days:
        return None
    stamps = days[max(days)]
    return min(stamps), max(stamps)


@dataclass(frozen=True, slots=True)
class Window:
    inside: bool  # inside a window now
    last_end: float | None  # end of the latest window that has ended
    next_start: float | None  # start of the next window
    next_end: float | None  # end of the next window that has not ended yet


def production_window(now: float, tz: tzinfo, production: Production | None, s: Settings) -> Window:
    """Where `now` is relative to the production windows of yesterday, today and tomorrow.

    The production day's times are moved by whole days of 86400 s, not by the local clock: the sun
    keeps UTC, so on the day the clocks change the window moves by an hour on the clock, like the
    sun does. Yesterday's window counts too: last generation + offset can be after midnight.
    Without production data there is no gating: always inside.
    """
    if production is None:
        return Window(True, None, None, None)
    first, last = production
    today = datetime.fromtimestamp(now, tz).date()
    shift = (today - datetime.fromtimestamp(first, tz).date()).days
    windows = [
        (first + days * DAY + s.start_offset, last + days * DAY + s.end_offset)
        for days in (shift - 1, shift, shift + 1)
    ]
    return Window(
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


def plan(now: float, tz: tzinfo, s: Settings, st: Status) -> Plan:
    today = datetime.fromtimestamp(now, tz).date()
    end_of_today = _at(today + timedelta(days=1), time(), tz)
    end_of_tomorrow = _at(today + timedelta(days=2), time(), tz)
    price_today = _at(today, s.price_time, tz)
    day = production_window(now, tz, st.production, s)
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
        tasks.add(Task.GENERATION)  # once more after the window: the last hours of generation
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
