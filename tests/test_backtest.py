"""The whole integration, day by day, on recorded data of random months (opt-in, slow: ~20 s per month).

    python tools/backtest_estimate.py --cache /some/dir      # downloads the Energy-Charts months once
    RMW_BACKTEST=/some/dir .venv/bin/python -m pytest -m backtest -s

The real coordinator runs in Home Assistant's test harness from the 20th of the month before, with the default
settings. Energy-Charts data is served as it is published (generation 2 h late, tomorrow's prices from 13:00), the
E-Control file contains the months published by then. Each evening at 23:55 the estimate sensor is compared with
the official value. RMW_BACKTEST_SEED picks the months (4 per season, 2023-01..2026-10; default: random; the
README's table: seed 15809), or RMW_BACKTEST_MONTHS names them (e.g. "2025-02,2023-12").
"""

import json
import os
import random
from datetime import date, datetime, time, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.referenzmarktwert import calc, sources
from custom_components.referenzmarktwert.const import (
    CONF_REQUEST_DELAY,
    DEFAULT_DATA,
    DEFAULT_OPTIONS,
    DOMAIN,
)

from .conftest import FIXTURES, VIENNA, econtrol_xlsx, fake_sources

CACHE = Path(os.environ["RMW_BACKTEST"]) if os.environ.get("RMW_BACKTEST") else None
SEED = int(os.environ.get("RMW_BACKTEST_SEED") or random.randrange(100000))
SEASONS = {"winter": (12, 1, 2), "spring": (3, 4, 5), "summer": (6, 7, 8), "autumn": (9, 10, 11)}
OFFICIAL = sources.parse_econtrol_xlsx((FIXTURES / "Referenzmarktwert_Entwicklung.xlsx").read_bytes())


def _season(month: calc.Month) -> str:
    return next(name for name, numbers in SEASONS.items() if month[1] in numbers)


def _months() -> list[tuple[str, calc.Month]]:
    if named := os.environ.get("RMW_BACKTEST_MONTHS"):
        months = [(int(text[:4]), int(text[5:7])) for text in named.split(",")]
        return [(_season(m), m) for m in months]
    rng = random.Random(SEED)
    candidates = [m for m in sorted(OFFICIAL) if (2023, 1) <= m <= (2026, 10)]
    return [
        (season, month)
        for season, numbers in SEASONS.items()
        for month in sorted(rng.sample([c for c in candidates if c[1] in numbers], 4))
    ]


def _load(month: calc.Month) -> tuple[list, list]:
    """A month from the backtest tool's cache: (generation, prices); empty if not downloaded."""
    file = CACHE / f"{month[0]}-{month[1]:02d}.json"
    if not file.exists():
        return [], []
    raw = json.loads(file.read_text())
    return [tuple(x) for x in raw["gen"]], [tuple(x) for x in raw["price"]]


pytestmark = [
    pytest.mark.backtest,
    pytest.mark.skipif(CACHE is None, reason="set RMW_BACKTEST to the backtest tool's cache directory"),
]


@pytest.mark.parametrize(("season", "month"), _months(), ids=[f"{s}-{m[0]}-{m[1]:02d}" for s, m in _months()])
async def test_backtest(hass, aioclient_mock, freezer, season: str, month: calc.Month) -> None:
    await hass.config.async_set_time_zone("Europe/Vienna")
    previous = calc.shift_month(month, -1)
    gen: set = set()
    price: set = set()
    for m in (calc.shift_month(month, -12), calc.shift_month(month, -2), previous, month, calc.shift_month(month, 1)):
        g, p = _load(m)
        gen.update(g)
        price.update(p)
    files: dict[calc.Month, bytes] = {}

    def econtrol(today: date) -> tuple[bytes, str]:
        """The E-Control file as published on `today`."""
        until = max(m for m, v in OFFICIAL.items() if v.published is not None and v.published <= today)
        if until not in files:
            files[until] = econtrol_xlsx(until)
        return files[until], format_datetime(datetime.combine(OFFICIAL[until].published, time(15), VIENNA))

    fake_sources(aioclient_mock, econtrol, sorted(gen), sorted(price))

    freezer.move_to(datetime(previous[0], previous[1], 20, 12, tzinfo=VIENNA))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Referenzmarktwert PV",
        data=dict(DEFAULT_DATA),
        options={**DEFAULT_OPTIONS, CONF_REQUEST_DELAY: 0},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    official = OFFICIAL[month].value * 100  # ct/kWh
    errors = []
    day = date(month[0], month[1], 1)
    while day.month == month[1]:
        target = datetime.combine(day, time(23, 55), VIENNA)
        while (current := datetime.now(VIENNA)) < target:
            freezer.tick(min(timedelta(minutes=5), target - current))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
        state = hass.states.get("sensor.referenzmarktwert_pv_estimate_running_month")
        errors.append(float(state.state) * 100 - official)
        day += timedelta(days=1)

    start, end = calc.month_bounds(month, VIENNA)
    floor = calc.market_value(*_load(month), start, end).eur_per_mwh / 10 - official
    settled = next((i + 1 for i in range(len(errors)) if all(abs(e) < 0.1 for e in errors[i:])), None)
    name = f"{season} {month[0]}-{month[1]:02d}" + ("" if os.environ.get("RMW_BACKTEST_MONTHS") else f" (seed {SEED})")
    print(f"\n{name}: official {official:.2f} ct/kWh, data floor {floor:+.3f}")
    print("error by day: " + " ".join(f"{d + 1}:{e:+.2f}" for d, e in enumerate(errors)))
    print(f"below 0.10 ct/kWh from day {settled}")
    # with the whole month downloaded the estimate is the month recomputed from the data: nothing was lost
    assert errors[-1] == pytest.approx(floor, abs=0.01)
