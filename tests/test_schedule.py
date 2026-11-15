"""When what is downloaded (schedule.py), and that skipping the night loses nothing."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.referenzmarktwert import sources
from custom_components.referenzmarktwert.coordinator import schedule_settings
from custom_components.referenzmarktwert.estimate import MonthData, estimate_month
from custom_components.referenzmarktwert.schedule import (
    Settings,
    Status,
    Task,
    jitter,
    plan,
    production_hours,
    production_window,
)

from .conftest import FIXTURES, fixture_json

TZ = ZoneInfo("Europe/Vienna")
S = Settings(
    interval=3600,
    start_offset=3600,
    end_offset=3 * 3600,
    full_refresh_time=time(2),
    price_time=time(13, 30),
    price_retry=1800,
    official_interval=86400,
    official_early_interval=3 * 3600,
    official_early_days=15,
)


def at(text: str) -> float:
    """A local (Vienna) time as Unix seconds."""
    return datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp()


# generation from 07:00 to 16:30 on the 15th: with the offsets of S the window is 08:00 .. 19:30 every day
PRODUCTION = (int(at("2026-11-15T07:00")), int(at("2026-11-15T16:30")))


def status(**changes) -> Status:
    """A settled state on 2026-11-16: full download and E-Control at 02:00, prices until the 17th."""
    values = {
        "generation_attempt": None,
        "full_refresh": at("2026-11-16T02:00"),
        "price_attempt": at("2026-11-16T13:30"),
        "prices_until": at("2026-11-18T00:00"),
        "reference_missing": False,
        "official_attempt": at("2026-11-16T02:00"),
        "official_failing": False,
        "official_awaited": False,
        "not_before": None,
        "production": PRODUCTION,
    }
    return Status(**{**values, **changes})


def test_fresh_start_fetches_everything() -> None:
    fresh = Status(None, None, None, None, True, None, False, True, None, None)
    assert plan(at("2026-11-16T23:00"), TZ, S, fresh).tasks == {
        Task.FULL,
        Task.PRICE,
        Task.REFERENCE,
        Task.OFFICIAL,
    }


def test_daytime_generation_every_interval() -> None:
    now = at("2026-11-16T12:00")
    assert plan(now, TZ, S, status(generation_attempt=now - 3601)).tasks == {Task.GENERATION}
    result = plan(now, TZ, S, status(generation_attempt=now - 1800))
    assert result.tasks == set()
    assert result.next_run == now + 1800  # when the interval is over


def test_night_downloads_nothing_and_sleeps_until_due() -> None:
    now = at("2026-11-16T22:00")
    result = plan(now, TZ, S, status(generation_attempt=at("2026-11-16T19:40")))
    assert result.tasks == set()
    assert result.next_run == at("2026-11-17T02:00")  # next: the nightly full download and E-Control


def test_one_more_download_after_the_window() -> None:
    """The last download in the window may be before its end: once more after it, then quiet."""
    now = at("2026-11-16T19:31")
    assert plan(now, TZ, S, status(generation_attempt=at("2026-11-16T19:00"))).tasks == {Task.GENERATION}
    assert plan(now, TZ, S, status(generation_attempt=now)).tasks == set()


def test_window_start_is_a_wake_up() -> None:
    now = at("2026-11-16T05:00")
    result = plan(now, TZ, S, status(generation_attempt=at("2026-11-16T02:00")))
    assert result.tasks == set()
    assert result.next_run == at("2026-11-16T08:00")


def test_nightly_full_download() -> None:
    now = at("2026-11-17T02:05")
    assert Task.FULL in plan(now, TZ, S, status()).tasks  # last full: yesterday 02:00
    assert plan(at("2026-11-17T01:55"), TZ, S, status()).next_run == at("2026-11-17T02:00")


def test_window_after_midnight() -> None:
    """Last generation + offset can be after midnight: yesterday's window still counts."""
    settings = replace(S, end_offset=8 * 3600)  # 16:30 + 8 h: until 00:30
    assert production_window(at("2026-11-17T00:15"), TZ, PRODUCTION, settings).inside
    assert not production_window(at("2026-11-17T00:45"), TZ, PRODUCTION, settings).inside


def test_no_production_data_means_no_gating() -> None:
    assert production_window(at("2026-11-16T03:00"), TZ, None, S).inside


def test_production_hours_of_the_latest_day_before_today() -> None:
    def point(text: str, value: float | None) -> tuple[int, float | None]:
        return int(at(text)), value

    series = [
        point("2026-11-14T06:45", 4.0),
        point("2026-11-14T16:45", 4.0),
        point("2026-11-15T05:00", 0.0),  # night: 0 is no generation
        point("2026-11-15T07:00", 4.0),
        point("2026-11-15T12:00", None),
        point("2026-11-15T16:30", 8.0),
        point("2026-11-15T20:00", 0.0),
        point("2026-11-16T06:00", 5.0),  # today: incomplete, not used
    ]
    assert production_hours(series, TZ, date(2026, 11, 16)) == PRODUCTION
    # yesterday missing (an outage): the latest day before it
    assert production_hours(series, TZ, date(2026, 11, 15)) == (
        int(at("2026-11-14T06:45")),
        int(at("2026-11-14T16:45")),
    )
    assert production_hours(series[2:3], TZ, date(2026, 11, 16)) is None  # only zeros


def test_window_follows_the_sun_over_the_clock_change() -> None:
    """Moved by 24 h of real time, not by the clock: on 25 Oct 2026 (CEST -> CET) the window is an
    hour earlier on the clock, as the sun is. Recorded data: generation on the 24th 06:00..19:30 CEST,
    on the 25th 05:30..18:00 CET."""
    generation = sources.parse_generation(fixture_json("public_power_at_2026-10.json"))
    production = production_hours(generation, TZ, date(2026, 10, 25))
    window = production_window(at("2026-10-25T12:00"), TZ, production, S)
    assert window.next_end == at("2026-10-25T18:30") + 3 * 3600  # 19:30 CEST = 18:30 CET, + 3 h
    assert not production_window(at("2026-10-25T05:59"), TZ, production, S).inside  # 06:00 CET + 1 h
    assert production_window(at("2026-10-25T06:00"), TZ, production, S).inside


def test_no_creeping_with_longer_days() -> None:
    """Each day the PV produces 45 min longer than the day before: more than the end offset leaves
    after the 2 h lag (30 min), so the evening's last values are published after the window closed.
    They are fetched before the next window is used (by the nightly full download, and again by the
    morning's first download, which starts at the last stored point): the window follows."""
    lag = 2 * 3600

    def generation_of(day: date) -> list[tuple[int, float | None]]:
        n = (day - date(2026, 3, 1)).days
        first = datetime.combine(day, time(7), TZ) - timedelta(minutes=45 * n)
        last = datetime.combine(day, time(17), TZ) + timedelta(minutes=45 * n)
        return [(int(first.timestamp()) + 900 * i, 5.0) for i in range(int((last - first).total_seconds()) // 900 + 1)]

    truth = [p for d in range(8) for p in generation_of(date(2026, 3, 1) + timedelta(days=d))]
    stored: dict[int, float | None] = {}
    checked: set[date] = set()
    settings = replace(S, start_offset=2 * 3600, end_offset=150 * 60)
    now = at("2026-03-02T00:30")
    st = status(full_refresh=now, generation_attempt=now, production=None)
    stored.update(p for p in truth if p[0] < now - lag)
    while now < at("2026-03-08T00:00"):
        today = datetime.fromtimestamp(now, TZ).date()
        st = replace(st, production=production_hours(sorted(stored.items()), TZ, today), price_attempt=now)
        tasks = plan(now, TZ, settings, st).tasks
        if tasks & {Task.FULL, Task.GENERATION}:
            stored.update(p for p in truth if p[0] <= now - lag)
            st = replace(st, generation_attempt=now, full_refresh=now if Task.FULL in tasks else st.full_refresh)
        if Task.OFFICIAL in tasks:
            st = replace(st, official_attempt=now)
        # while the window is in use, it comes from yesterday's complete generation
        if production_window(now, TZ, st.production, settings).inside:
            complete = production_hours(generation_of(today - timedelta(days=1)), TZ, today)
            assert production_hours(sorted(stored.items()), TZ, today) == complete
            checked.add(today)
        now = plan(now, TZ, settings, st).next_run
    assert len(checked) == 6  # every day


@pytest.mark.parametrize(
    ("now", "prices_until", "due"),
    [
        ("2026-11-16T03:00", "2026-11-16T12:00", True),  # today's prices missing: at once
        ("2026-11-16T12:00", "2026-11-17T00:00", False),  # today's there, tomorrow's not published yet
        ("2026-11-16T13:31", "2026-11-17T00:00", True),  # after the auction
        ("2026-11-16T15:00", "2026-11-18T00:00", False),  # tomorrow's there
    ],
)
def test_prices(now: str, prices_until: str, due: bool) -> None:
    st = status(prices_until=at(prices_until), price_attempt=at(now) - 3600)
    assert (Task.PRICE in plan(at(now), TZ, S, st).tasks) is due


def test_price_retry_and_next_day() -> None:
    now = at("2026-11-16T13:40")
    result = plan(now, TZ, S, status(prices_until=at("2026-11-17T00:00"), price_attempt=now - 600))
    assert Task.PRICE not in result.tasks
    assert result.next_run == now + 1200  # 30 min after the last attempt
    done = plan(at("2026-11-16T15:00"), TZ, S, status(prices_until=at("2026-11-18T00:00")))
    assert done.next_run <= at("2026-11-17T08:00")


@pytest.mark.parametrize(
    ("now", "awaited", "failing", "hours"),
    [
        ("2026-11-03T12:00", True, False, 3),  # awaited, early in the month
        ("2026-11-16T12:00", True, False, 24),  # after the 15th: daily again
        ("2026-11-03T12:00", False, False, 24),  # already published
        ("2026-11-16T12:00", False, True, 1),  # failing: every interval
    ],
)
def test_official_interval(now: str, awaited: bool, failing: bool, hours: int) -> None:
    def due(after: float) -> bool:
        st = status(official_attempt=at(now) - after, official_awaited=awaited, official_failing=failing)
        return Task.OFFICIAL in plan(at(now), TZ, S, st).tasks

    assert not due(hours * 3600 - 60)
    assert due(hours * 3600)


def test_rate_limit_blocks_energy_charts_only() -> None:
    now = at("2026-11-16T12:00")
    st = status(generation_attempt=now - 7200, official_attempt=now - 90000, not_before=now + 600)
    result = plan(now, TZ, S, st)
    assert result.tasks == {Task.OFFICIAL}
    assert result.next_run == now + 600


def test_month_change_is_a_wake_up() -> None:
    now = at("2026-11-30T23:00")
    st = status(generation_attempt=now, prices_until=at("2026-12-02T00:00"), official_attempt=now)
    assert plan(now, TZ, S, st).next_run == at("2026-12-01T00:00")


def test_never_more_often_than_a_minute() -> None:
    now = at("2026-11-16T12:00")
    st = status(generation_attempt=now - 3599)
    assert plan(now, TZ, S, st).next_run == now + 60


# --- no loss of detail -----------------------------------------------------------------------------


def test_skipping_the_night_changes_almost_nothing() -> None:
    """October 2026 as known between the evening's last and the next morning's first download.

    With the default offsets a point counts as fetched if it is published (~2 h after its time)
    inside the day's window, which comes from the day before. Measured: 10 points of 4-8 MW
    (0.003 % of the energy) fall outside, on days the PV produced longer than the day before. The
    morning ones still arrive with the window's first download (it starts at the last stored point
    minus the overlap), the evening ones with the nightly full download.
    """
    september = sources.parse_generation(fixture_json("public_power_at_2026-09.json"))
    generation = sources.parse_generation(fixture_json("public_power_at_2026-10.json"))
    price = sources.parse_price(fixture_json("price_at_2026-10.json"))
    reference = MonthData(sources.parse_generation(fixture_json("public_power_at_2025-10.json")), [])
    official = sources.parse_econtrol_xlsx((FIXTURES / "Referenzmarktwert_Entwicklung.xlsx").read_bytes())
    settings = schedule_settings({})  # the defaults
    lag = 2 * 3600

    windows = {}  # the window of each day, from the day before

    def fetched_in_window(ts: int) -> bool:
        day = datetime.fromtimestamp(ts + lag, TZ).date()
        if day not in windows:
            windows[day] = production_hours(september + generation, TZ, day)
        return production_window(ts + lag, TZ, windows[day], settings).inside

    daytime = [(ts, v) for ts, v in generation if fetched_in_window(ts)]
    assert len(daytime) < len(generation) * 0.75  # a real cut
    missing = sum(v for ts, v in generation if v and not fetched_in_window(ts))
    assert missing / sum(v for _, v in generation if v) < 0.0001

    now = at("2026-10-31T23:30")
    values = {m: v.value for m, v in official.items() if m < (2026, 10)}
    bases = {m: v.base for m, v in official.items() if m < (2026, 10) and v.base is not None}
    full = estimate_month(now, TZ, MonthData(generation, price), reference, values, bases)
    cut = estimate_month(now, TZ, MonthData(daytime, price), reference, values, bases)
    assert full.month_so_far == pytest.approx(0.1771, abs=0.0001)
    assert cut.month_so_far == pytest.approx(full.month_so_far, abs=0.00001)  # < 0.001 ct/kWh
    assert cut.known_share == pytest.approx(full.known_share)


# --- spread ---------------------------------------------------------------------------------------

SPREAD = replace(S, spread=600, seed="entry-a")  # +-5 min


def test_jitter_stays_in_range_and_changes_daily() -> None:
    days = [date(2026, 1, 1) + timedelta(days=n) for n in range(365)]
    shifts = [jitter(SPREAD, d) for d in days]
    assert all(-300 <= x < 300 for x in shifts)
    assert min(shifts) < -250 and max(shifts) > 250  # the whole range is used
    assert len(set(shifts)) == len(shifts)  # new every day: no fixed pattern
    assert jitter(SPREAD, days[0]) == jitter(replace(SPREAD), days[0])  # the same after a restart
    assert jitter(replace(SPREAD, seed="entry-b"), days[0]) != shifts[0]  # differs between installations
    assert jitter(S, days[0]) == 0.0  # spread 0: off


def test_planned_times_move_by_the_days_jitter() -> None:
    day = date(2026, 11, 16)
    moved = jitter(SPREAD, day)
    assert moved != 0
    # the window starts later or earlier, the same for its end
    window = production_window(at("2026-11-16T05:00"), TZ, PRODUCTION, SPREAD)
    assert window.next_start == at("2026-11-16T08:00") + moved
    assert window.next_end == at("2026-11-16T19:30") + moved
    # nightly full download
    assert plan(at("2026-11-16T01:50"), TZ, SPREAD, status(full_refresh=at("2026-11-15T02:00"))).next_run == (
        at("2026-11-16T02:00") + moved
    )
    # tomorrow's prices
    st = status(
        prices_until=at("2026-11-17T00:00"),
        price_attempt=at("2026-11-16T03:00"),
        generation_attempt=at("2026-11-16T12:50"),  # the next generation download only at 13:50
    )
    assert plan(at("2026-11-16T13:00"), TZ, SPREAD, st).next_run == at("2026-11-16T13:30") + moved


def test_reference_month_waits_for_the_spread_after_midnight() -> None:
    """At a month change every installation needs last year's month: not all at midnight."""
    first = date(2026, 12, 1)
    slot = at("2026-12-01T00:00") + jitter(SPREAD, first) + 300  # 0 .. 10 min after midnight
    st = status(reference_missing=True, generation_attempt=at("2026-11-30T23:00"), prices_until=at("2026-12-02T00:00"))
    early = plan(at("2026-12-01T00:00"), TZ, SPREAD, st)
    assert Task.REFERENCE not in early.tasks
    assert early.next_run == max(slot, at("2026-12-01T00:01"))  # at least a minute between runs
    assert Task.REFERENCE in plan(slot, TZ, SPREAD, st).tasks
    assert Task.REFERENCE in plan(at("2026-12-01T00:00"), TZ, S, st).tasks  # spread 0: at once


@pytest.mark.parametrize("clock", [time(0, 0), time(23, 58), time(0, 3)])
@pytest.mark.parametrize("seed", ["entry-a", "entry-b"])
def test_full_download_near_midnight_runs_once_per_day(clock: time, seed: str) -> None:
    """With the spread a slot near midnight can fall on the neighbouring calendar day: still exactly one
    full download per slot, never before it."""
    settings = replace(S, full_refresh_time=clock, spread=600, seed=seed)
    first = date(2026, 3, 20)  # across the clock change on 29 March
    slots = [
        datetime.combine(first + timedelta(days=n), clock, TZ).timestamp() + jitter(settings, first + timedelta(days=n))
        for n in range(1, 60)
    ]
    now = slots[0] - 12 * 3600
    st = status(full_refresh=now, generation_attempt=now, official_attempt=now, production=PRODUCTION)
    runs = []
    while now < slots[-1] + 3600:
        st = replace(st, price_attempt=now, prices_until=now + 2 * 86400, official_attempt=now)
        tasks = plan(now, TZ, settings, st).tasks
        if Task.FULL in tasks:
            runs.append(now)
            st = replace(st, full_refresh=now, generation_attempt=now)
        elif Task.GENERATION in tasks:
            st = replace(st, generation_attempt=now)
        now = plan(now, TZ, settings, st).next_run
    assert len(runs) == len(slots)  # once per day: none missed, none twice
    assert all(0 <= run - slot <= 60 for run, slot in zip(runs, slots, strict=True))  # at its slot
