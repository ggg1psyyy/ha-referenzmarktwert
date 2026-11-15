"""Tests for the pure calculation module.

Fixtures: Energy-Charts responses for Sep/Oct 2026 (Austria, Solar + day-ahead price),
data © Fraunhofer ISE Energy-Charts / Bundesnetzagentur SMARD.de, CC BY 4.0.
"""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.referenzmarktwert import calc

FIXTURES = Path(__file__).parent / "fixtures"
VIENNA = ZoneInfo("Europe/Vienna")


def month_bounds(year: int, month: int) -> tuple[int, int]:
    """[start, end) of a month in Vienna local time, as Unix seconds."""
    start = datetime(year, month, 1, tzinfo=VIENNA)
    end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=VIENNA)
    return int(start.timestamp()), int(end.timestamp())


def load_series(month: str) -> tuple[list[tuple[int, float | None]], list[tuple[int, float | None]]]:
    gen = json.loads((FIXTURES / f"public_power_at_{month}.json").read_text())
    price = json.loads((FIXTURES / f"price_at_{month}.json").read_text())
    solar = next(p for p in gen["production_types"] if p["name"] == "Solar")
    return (
        list(zip(gen["unix_seconds"], solar["data"])),
        list(zip(price["unix_seconds"], price["price"])),
    )


@pytest.mark.parametrize(
    ("year", "month", "official_ct"),
    [(2026, 9, 10.53), (2026, 10, 17.71)],
)
def test_reproduces_official_value(year: int, month: int, official_ct: float) -> None:
    gen, price = load_series(f"{year}-{month:02d}")
    start, end = month_bounds(year, month)
    result = calc.market_value(gen, price, start, end)
    assert result is not None
    assert round(result.ct_per_kwh, 2) == official_ct
    assert result.last_period == end - 3600  # last hour of the month, nothing from the next one


def test_quarter_hour_weighting_differs() -> None:
    """E-Control aggregates to hours first; weighting quarter-hours gives a wrong value."""
    gen, price = load_series("2026-10")
    start, end = month_bounds(2026, 10)
    result = calc.market_value(gen, price, start, end, period=900)
    assert result is not None
    assert round(result.ct_per_kwh, 2) != 17.71
    assert result.ct_per_kwh == pytest.approx(17.54, abs=0.01)


def test_aggregate_half_open_window_and_none() -> None:
    series = [(0, 1.0), (900, None), (1800, 3.0), (3600, 10.0), (7200, 99.0)]
    assert calc.aggregate(series, start=0, end=7200) == {0: 2.0, 3600: 10.0}


def test_weighted_value_negative_prices_count() -> None:
    gen = {0: 1.0, 3600: 3.0}
    price = {0: -20.0, 3600: 100.0}
    result = calc.weighted_value(gen, price)
    assert result is not None
    assert result.eur_per_mwh == pytest.approx((-20 * 1 + 100 * 3) / 4)
    assert result.energy_mwh == 4.0
    assert result.periods == 2


def test_weighted_value_skips_hours_without_price_and_handles_no_generation() -> None:
    assert calc.weighted_value({0: 2.0, 3600: 5.0}, {0: 50.0}).eur_per_mwh == 50.0
    assert calc.weighted_value({0: 0.0}, {0: 50.0}) is None
    assert calc.weighted_value({}, {}) is None


def test_elapsed_share() -> None:
    ref = {1000: 1.0, 4600: 3.0, 8200: 4.0}  # offsets 0, 3600, 7200 from start 1000
    assert calc.elapsed_share(ref, 1000, 0) == 0.0
    assert calc.elapsed_share(ref, 1000, 3600) == 1 / 8
    assert calc.elapsed_share(ref, 1000, 7200) == 4 / 8
    assert calc.elapsed_share(ref, 1000, 10**9) == 1.0
    assert calc.elapsed_share({}, 0, 3600) is None
    assert calc.elapsed_share({0: 0.0}, 0, 3600) is None


def test_blend() -> None:
    assert calc.blend(20.0, 10.0, 0.25) == pytest.approx(12.5)
    assert calc.blend(20.0, 10.0, 1.5) == 20.0  # share clamped
    assert calc.blend(None, 10.0, 0.5) == 10.0
    assert calc.blend(20.0, None, 0.1) == 20.0
    assert calc.blend(None, None, 0.5) is None
    assert calc.blend(20.0, 10.0, None) == 20.0  # unknown share: keep the real data


def test_payout() -> None:
    assert calc.payout(17.71, 1.5) == pytest.approx(16.21)
