"""When what is downloaded (schedule.py), and that skipping the night loses nothing."""

from dataclasses import replace
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from astral.sun import sunrise, sunset

from custom_components.referenzmarktwert import sources
from custom_components.referenzmarktwert.estimate import MonthData, estimate_month
from custom_components.referenzmarktwert.schedule import Settings, Status, Task, daylight, plan

from .conftest import FIXTURES, VIENNA_CITY, fixture_json

TZ = ZoneInfo("Europe/Vienna")
S = Settings(
    interval=3600,
    sunrise_offset=3600,
    sunset_offset=3 * 3600,
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


def fixed_sun(day: date) -> tuple[datetime, datetime]:
    """Sunrise 07:00, sunset 16:30 every day: window 08:00 .. 19:30."""
    return datetime.combine(day, time(7), TZ), datetime.combine(day, time(16, 30), TZ)


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
    }
    return Status(**{**values, **changes})


def test_fresh_start_fetches_everything() -> None:
    fresh = Status(None, None, None, None, True, None, False, True, None)
    assert plan(at("2026-11-16T23:00"), TZ, fixed_sun, S, fresh).tasks == {
        Task.FULL,
        Task.PRICE,
        Task.REFERENCE,
        Task.OFFICIAL,
    }


def test_daytime_generation_every_interval() -> None:
    now = at("2026-11-16T12:00")
    assert plan(now, TZ, fixed_sun, S, status(generation_attempt=now - 3601)).tasks == {Task.GENERATION}
    result = plan(now, TZ, fixed_sun, S, status(generation_attempt=now - 1800))
    assert result.tasks == set()
    assert result.next_run == now + 1800  # when the interval is over


def test_night_downloads_nothing_and_sleeps_until_due() -> None:
    now = at("2026-11-16T22:00")
    result = plan(now, TZ, fixed_sun, S, status(generation_attempt=at("2026-11-16T19:40")))
    assert result.tasks == set()
    assert result.next_run == at("2026-11-17T02:00")  # next: the nightly full download and E-Control


def test_one_more_download_after_the_window() -> None:
    """The last download in the window may be before its end: once more after it, then quiet."""
    now = at("2026-11-16T19:31")
    assert plan(now, TZ, fixed_sun, S, status(generation_attempt=at("2026-11-16T19:00"))).tasks == {Task.GENERATION}
    assert plan(now, TZ, fixed_sun, S, status(generation_attempt=now)).tasks == set()


def test_window_start_is_a_wake_up() -> None:
    now = at("2026-11-16T05:00")
    result = plan(now, TZ, fixed_sun, S, status(generation_attempt=at("2026-11-16T02:00")))
    assert result.tasks == set()
    assert result.next_run == at("2026-11-16T08:00")


def test_nightly_full_download() -> None:
    now = at("2026-11-17T02:05")
    assert Task.FULL in plan(now, TZ, fixed_sun, S, status()).tasks  # last full: yesterday 02:00
    assert plan(at("2026-11-17T01:55"), TZ, fixed_sun, S, status()).next_run == at("2026-11-17T02:00")


def test_window_after_midnight() -> None:
    """In summer sunset + offset can be after midnight: yesterday's window still counts."""
    settings = replace(S, sunset_offset=8 * 3600)  # 16:30 + 8 h: until 00:30
    assert daylight(at("2026-11-17T00:15"), TZ, fixed_sun, settings).inside
    assert not daylight(at("2026-11-17T00:45"), TZ, fixed_sun, settings).inside


def test_no_sun_times_means_no_gating() -> None:
    result = daylight(at("2026-11-16T03:00"), TZ, lambda _: None, S)
    assert result.inside


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
    assert (Task.PRICE in plan(at(now), TZ, fixed_sun, S, st).tasks) is due


def test_price_retry_and_next_day() -> None:
    now = at("2026-11-16T13:40")
    result = plan(now, TZ, fixed_sun, S, status(prices_until=at("2026-11-17T00:00"), price_attempt=now - 600))
    assert Task.PRICE not in result.tasks
    assert result.next_run == now + 1200  # 30 min after the last attempt
    done = plan(at("2026-11-16T15:00"), TZ, fixed_sun, S, status(prices_until=at("2026-11-18T00:00")))
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
        return Task.OFFICIAL in plan(at(now), TZ, fixed_sun, S, st).tasks

    assert not due(hours * 3600 - 60)
    assert due(hours * 3600)


def test_rate_limit_blocks_energy_charts_only() -> None:
    now = at("2026-11-16T12:00")
    st = status(generation_attempt=now - 7200, official_attempt=now - 90000, not_before=now + 600)
    result = plan(now, TZ, fixed_sun, S, st)
    assert result.tasks == {Task.OFFICIAL}
    assert result.next_run == now + 600


def test_month_change_is_a_wake_up() -> None:
    now = at("2026-11-30T23:00")
    st = status(generation_attempt=now, prices_until=at("2026-12-02T00:00"), official_attempt=now)
    assert plan(now, TZ, fixed_sun, S, st).next_run == at("2026-12-01T00:00")


def test_never_more_often_than_a_minute() -> None:
    now = at("2026-11-16T12:00")
    st = status(generation_attempt=now - 3599)
    assert plan(now, TZ, fixed_sun, S, st).next_run == now + 60


# --- no loss of detail -----------------------------------------------------------------------------


def test_skipping_the_night_changes_almost_nothing() -> None:
    """October 2026 as known between the evening's last and the next morning's first download.

    The data only lacks what Energy-Charts publishes outside the daylight windows (Vienna): a few
    quarter-hours of 4-8 MW around dawn and dusk, 0.05 % of the month's energy. The estimate moves
    by less than 0.005 ct/kWh, and only until the next download brings them (it starts at the last
    stored point minus the overlap, see test_init).
    """
    observer = VIENNA_CITY.observer

    def sun(day: date) -> tuple[datetime, datetime]:
        return sunrise(observer, day, TZ), sunset(observer, day, TZ)

    generation = sources.parse_generation(fixture_json("public_power_at_2026-10.json"))
    price = sources.parse_price(fixture_json("price_at_2026-10.json"))
    reference = MonthData(sources.parse_generation(fixture_json("public_power_at_2025-10.json")), [])
    official = sources.parse_econtrol_xlsx((FIXTURES / "Referenzmarktwert_Entwicklung.xlsx").read_bytes())
    # a point is published ~2 h after its time: it arrives if that is inside a window
    lag = 2 * 3600
    daytime = [(ts, v) for ts, v in generation if daylight(ts + lag, TZ, sun, S).inside]
    assert len(daytime) < len(generation) * 0.75  # a real cut
    missing = sum(v for ts, v in generation if v and not daylight(ts + lag, TZ, sun, S).inside)
    assert missing / sum(v for _, v in generation if v) < 0.001

    now = at("2026-10-31T23:30")
    values = {m: v.value for m, v in official.items() if m < (2026, 10)}
    bases = {m: v.base for m, v in official.items() if m < (2026, 10) and v.base is not None}
    full = estimate_month(now, TZ, MonthData(generation, price), reference, values, bases)
    cut = estimate_month(now, TZ, MonthData(daytime, price), reference, values, bases)
    assert full.month_so_far == pytest.approx(0.1771, abs=0.0001)
    assert cut.month_so_far == pytest.approx(full.month_so_far, abs=0.00005)  # < 0.005 ct/kWh
    assert cut.known_share == pytest.approx(full.known_share)
