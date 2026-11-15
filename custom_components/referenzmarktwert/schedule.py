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

Fixed times of day (window start and end, the nightly full download, the price time, the reference
month after a month change) are moved by `jitter()`: a random amount within `spread`, new every day,
so that installations with the same settings do not all ask Energy-Charts at the same second.

`plan()` returns the due tasks and when the next one becomes due, so the coordinator wakes up
exactly then (also at night, when only prices, E-Control or the month change may be due).
"""

import hashlib
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
    spread: float = 0.0  # planned times move by up to +-spread/2 (0: off)
    seed: str = ""  # per installation (the config entry's random id), so the shifts differ


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


def jitter(s: Settings, day: date) -> float:
    """The day's shift of the planned times, -spread/2 .. +spread/2 seconds.

    Pseudo-random from the seed and the date: new every day (no fixed pattern that would mark an
    installation), the same all day and after a restart, nothing to store.
    """
    if s.spread <= 0:
        return 0.0
    digest = hashlib.sha256(f"{s.seed}:{day.isoformat()}".encode()).digest()
    return (int.from_bytes(digest[:8]) / 2**64 - 0.5) * s.spread


def _slot(day: date, clock: time, tz: tzinfo, s: Settings) -> float:
    """A planned time of day on `day`, shifted by the day's jitter."""
    return _at(day, clock, tz) + jitter(s, day)


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
    windows = []
    for days in (shift - 1, shift, shift + 1):
        moved = jitter(s, today + timedelta(days=days - shift))  # the shift of the window's own day
        windows.append((first + days * DAY + s.start_offset + moved, last + days * DAY + s.end_offset + moved))
    return Window(
        inside=any(start <= now < end for start, end in windows),
        last_end=max((end for _, end in windows if end <= now), default=None),
        next_start=min((start for start, _ in windows if start > now), default=None),
        next_end=min((end for _, end in windows if end > now), default=None),
    )


def _slots(now: float, clock: time, tz: tzinfo, s: Settings) -> tuple[float, float]:
    """The latest slot of `clock` (local, shifted by its day's jitter) at or before `now`, and the next one.

    Near midnight a shifted slot can fall on the neighbouring calendar day (00:00 - 3 min is the
    evening before, 23:58 + 4 min the next morning), so the slots of yesterday, today and tomorrow
    are compared by time, not picked by date.
    """
    today = datetime.fromtimestamp(now, tz).date()
    slots = sorted(_slot(today + timedelta(days=d), clock, tz, s) for d in (-2, -1, 0, 1, 2))
    return max(x for x in slots if x <= now), min(x for x in slots if x > now)


def _elapsed(since: float | None, now: float, wait: float) -> bool:
    return since is None or now - since >= wait


def plan(now: float, tz: tzinfo, s: Settings, st: Status) -> Plan:
    today = datetime.fromtimestamp(now, tz).date()
    end_of_today = _at(today + timedelta(days=1), time(), tz)
    end_of_tomorrow = _at(today + timedelta(days=2), time(), tz)
    price_today = _slot(today, s.price_time, tz, s)
    day = production_window(now, tz, st.production, s)
    last_full_slot, next_full_slot = _slots(now, s.full_refresh_time, tz, s)
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)

    tasks: set[Task] = set()
    wake: list[float] = [next_full_slot]

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
        wake.append(_slot(today + timedelta(days=1), s.price_time, tz, s))

    # --- reference month: after a month change only after the day's shift (0 .. spread after midnight)
    def reference_slot(first_day: date) -> float:
        return _at(first_day, time(), tz) + jitter(s, first_day) + s.spread / 2

    if st.reference_missing:
        if now >= reference_slot(month_start):
            tasks.add(Task.REFERENCE)
        else:
            wake.append(reference_slot(month_start))
    wake.append(reference_slot(next_month))

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
    wake.append(_at(next_month, time(), tz))

    # --- Energy-Charts asked us to wait
    if st.not_before is not None and now < st.not_before:
        tasks -= ENERGY_CHARTS_TASKS
        wake.append(st.not_before)

    future = [w for w in wake if w > now]
    next_run = min(future, default=now + MAX_WAIT)
    next_run = min(max(next_run, now + MIN_WAIT), now + MAX_WAIT)
    return Plan(frozenset(tasks), next_run)
