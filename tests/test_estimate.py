"""Tests for the running-month estimate (pure, recorded Energy-Charts data, CC BY 4.0)."""

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.referenzmarktwert import calc, sources
from custom_components.referenzmarktwert.estimate import (
    MonthData,
    PreviousSource,
    estimate_month,
    recompute_month,
)

FIXTURES = Path(__file__).parent / "fixtures"
VIENNA = ZoneInfo("Europe/Vienna")
OCT_START, OCT_END = calc.month_bounds((2026, 10), VIENNA)


def load(month: str, with_price: bool = True) -> MonthData:
    gen = sources.parse_generation(json.loads((FIXTURES / f"public_power_at_{month}.json").read_text()))
    price = sources.parse_price(json.loads((FIXTURES / f"price_at_{month}.json").read_text())) if with_price else []
    return MonthData(gen, price)


@pytest.fixture(scope="module")
def official() -> dict:
    return sources.parse_econtrol_xlsx((FIXTURES / "Referenzmarktwert_Entwicklung.xlsx").read_bytes())


def cut(data: MonthData, generation_until: int, prices_until: int) -> MonthData:
    """What Energy-Charts would have returned at that time."""
    return MonthData(
        [(t, v) for t, v in data.generation if t < generation_until],
        [(t, v) for t, v in data.price if t < prices_until],
    )


def tables(official: dict, until: calc.Month) -> tuple[dict, dict]:
    values = {m: v.value for m, v in official.items() if m <= until}
    bases = {m: v.base for m, v in official.items() if m <= until and v.base is not None}
    return values, bases


def test_month_bounds_and_month_of() -> None:
    assert OCT_END - OCT_START == 31 * 86400 + 3600  # October has the extra DST hour
    assert calc.month_of(OCT_START, VIENNA) == (2026, 10)
    assert calc.month_of(OCT_END - 1, VIENNA) == (2026, 10)
    assert calc.month_of(OCT_END, VIENNA) == (2026, 11)


def test_complete_month_gives_official_value(official: dict) -> None:
    """At the very end the estimate is the month so far, i.e. the recomputed official value."""
    values, bases = tables(official, (2026, 9))
    result = estimate_month(OCT_END - 1, VIENNA, load("2026-10"), load("2025-10", False), values, bases)
    assert result.known_share == 1.0
    assert result.value == pytest.approx(0.1771, abs=0.00005)
    assert result.generation_until == OCT_END  # the inclusive end point is not used
    assert result.previous_source is PreviousSource.OFFICIAL
    assert result.previous_month == pytest.approx(0.1053)


def test_day_ten_blends_prior_and_month_so_far(official: dict) -> None:
    now = OCT_START + 10 * 86400
    data = cut(load("2026-10"), now - 2 * 3600, now + 86400)
    values, bases = tables(official, (2026, 9))
    result = estimate_month(now, VIENNA, data, load("2025-10", False), values, bases)
    assert result.generation_until == now - 2 * 3600
    assert result.prices_until == now + 86400
    assert 0 < result.known_share < 0.5
    expected = calc.blend(result.month_so_far, result.prior, result.known_share)
    assert result.value == pytest.approx(expected)
    # the prior: price level between September's and October's known base, x Oct 2025 capture rate
    capture = official[(2025, 10)].value / official[(2025, 10)].base
    low, high = sorted([official[(2026, 9)].base * capture, result.running_base * capture])
    assert low <= result.prior <= high
    # Cross-checked with tools/backtest_estimate.py's code path: 13.98 ct. October 2026 is a hard month
    # (official 17.71, +7.2 ct on September): its first ten days were cheap, prices rose later.
    assert result.value == pytest.approx(0.13981, abs=0.00001)


def test_partial_hour_is_not_used(official: dict) -> None:
    now = OCT_START + 5 * 86400
    data = cut(load("2026-10"), now - 2 * 3600 - 1800, now)  # last hour has only 2 quarter-hours
    values, bases = tables(official, (2026, 9))
    result = estimate_month(now, VIENNA, data, load("2025-10", False), values, bases)
    assert result.generation_until == now - 3 * 3600


def test_previous_month_recomputed_until_published(official: dict) -> None:
    now = OCT_START + 1 * 86400  # 2 October: September not yet published
    data = cut(load("2026-10"), now - 2 * 3600, now + 86400)
    values, bases = tables(official, (2026, 8))
    recomputed = recompute_month(load("2026-09"), (2026, 9), VIENNA)
    assert recomputed[0] == pytest.approx(0.1053, abs=0.00005)
    result = estimate_month(now, VIENNA, data, load("2025-10", False), values, bases, recomputed)
    assert result.previous_source is PreviousSource.RECOMPUTED
    assert result.previous_month == recomputed[0]


def test_missing_data_degrades_gracefully(official: dict) -> None:
    now = OCT_START + 3600  # first hour of the month: nothing known yet
    result = estimate_month(now, VIENNA, MonthData([], []), None, {}, {})
    assert result.value is None and result.month_so_far is None
    assert result.previous_source is PreviousSource.MISSING
    # no reference (last year's data failed): the month so far is used as is
    now = OCT_START + 10 * 86400
    values, bases = tables(official, (2026, 9))
    result = estimate_month(now, VIENNA, cut(load("2026-10"), now, now), None, values, bases)
    assert result.known_share is None
    assert result.value == result.month_so_far
