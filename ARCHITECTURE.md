# Architecture and decisions

The README describes the current state; this file records *why* things are the way they are, and the history.

## Goal

1. Calculate the expected Referenzmarktwert (§ 13 EAG) of the running month from public data, update it as data
   arrives, and expose it — together with the latest official monthly value and the resulting payout — as sensors.
2. Value grid export hour by hour with that price and write the compensation as an **external statistic** for the
   Energy Dashboard, rewritten for the running month as the estimate improves and a final time when the official value
   is published (no correction spikes, history always right).

This integration is the single source of the price: consumers (e.g. a PV controller) only read its sensors.

## How the price is defined

- E-Control: volume-weighted mean of the **hourly** day-ahead prices of the Austrian bidding zone, weighted by the
  **hourly generation of the technology in the whole Austrian control area**.
- Since 2025-10-01 prices and generation are quarter-hourly, but the law says hourly: E-Control first aggregates to
  hours (price: mean of the four quarters; generation: the hour's energy), then weights. Weighting quarter-hours
  directly gives a measurably different value (Oct 2026: 17.54 instead of 17.71 ct/kWh) — a test pins this.
- Negative hours count normally.
- Official values are published at the start of the following month (observed: 2nd–7th).
- Example tariff: Hofer Grünstrom "Sonne" = Referenzmarktwert PV − 1.5 ct/kWh (CPI-indexed → a setting).
  It is *not* the OeMAG market price, which is a different price and cannot be recomputed.

## Data sources

- **Energy-Charts** (no API key, CC BY 4.0): `public_power?country=at` (15-min generation, `name == "Solar"`) and
  `price?bzn=AT` (15-min EUR/MWh). The `end` parameter is **inclusive**, so a month request also returns the first
  point of the next month — the calculation uses a half-open `[start, end)` window. Generation lags real time by ~2 h.
  Plain Unix seconds are accepted for `start`/`end` (no URL-encoding of `+02:00` offsets needed). The API
  **rate-limits**: an HTTP 429 was observed during a few quick requests in a row → `SourceRateLimitError` carries
  `Retry-After`; the coordinator must poll sparingly and back off.
- **E-Control** history file `Referenzmarktwert_Entwicklung.xlsx` (monthly since 2022-01; columns: month, hydro,
  wind, PV, base price, publication date; a unit row "Cent/kWh"). Parsed with the standard library (it is a zip of
  XML) — no openpyxl. The first sheet is resolved via `workbook.xml` + relationships instead of assuming
  `sheet1.xml`; the unit is read from the header and converted to EUR/kWh (an unknown unit is an error, not a silent
  factor 100); oversized XML parts are refused, and any damage (bad zip, bad XML, bad indexes) is a
  `SourceDataError`. The test fixture is the real file minus metadata (author names, printer settings, internal path).

`sources.py` design: parsing (pure, on plain data) is separate from fetching; fetching takes an
`aiohttp.ClientSession` from the caller (in HA: the shared session), so it is tested against a local aiohttp test
server. All failures become `SourceError` subclasses (`SourceConnectionError`, `SourceRateLimitError`,
`SourceDataError`). URLs, paths, country, bidding zone, production type, columns, extra headers (e.g. an API key) and
the timeout are fields of `EnergyChartsConfig` / `EControlConfig`.

Recomputation check against official values: Sep 2026 10.529 (10.53), Oct 2026 17.712 (17.71), Apr 2026 1.596 (1.70,
likely later corrections by APG in a month with many negative hours).

## Prior art

- **ha-oemag**: OeMAG price only, but its statistics mechanism is the model — hourly compensation written as an
  external statistic, re-importing rows with an existing start overwrites them (past rewritten without spikes), a
  rebuild service, pure modules without HA imports. HA's own "entity with current rate" cost sensor can never be
  corrected afterwards, which is why an external statistic is used.
- **EEGEnergyOptimizer2**: a full battery optimizer; not usable as just a price source.
- **homeassistant-energy-charts**: production data only.

## Design

```
custom_components/referenzmarktwert/
  const.py         # defaults only (no HA imports)
  calc.py          # pure calculation
  sources.py       # Energy-Charts client, E-Control xlsx parser
  statistics.py    # hourly compensation -> external statistic rows
  coordinator.py   # DataUpdateCoordinator
  sensor.py, config_flow.py, services.yaml, manifest.json, translations/, diagnostics.py
tests/             # pytest, recorded responses as fixtures
```

### Decisions

- **Everything configurable.** Every URL, calculation parameter, login/API key and interval is user-configurable.
  Defaults live in `const.py`; logic functions take them as parameters instead of reading module constants.
- **Pure core.** `calc.py` has no HA, network or clock dependency, so it is tested in isolation.
- **Units: EUR, never cents.** Internally EUR/MWh (as delivered by the exchange) and MW; everything user-facing
  (sensors, deduction setting, statistics) is in EUR/kWh. Reason: the Energy Dashboard only checks that a price
  unit ends in `/kWh` (or another energy unit) and multiplies it by energy into the instance currency — a `ct/kWh`
  sensor would pass validation but book costs 100x too high. E-Control's ct/kWh values are converted on import.
- **Whole-month estimate** = month so far, blended with a *prior* for the rest of the month; the weight is the share
  of the month's *expected PV energy* already passed (reference: the same month last year). Weighted by PV energy, not
  days. If the share is unknown (reference data missing) the month so far is used as is — real data is never
  discarded. The elapsed time is taken from the latest data point, not the wall clock (generation lags ~2 h).
- **Prior = price level × last year's capture rate of this calendar month** (capture rate = PV value / day-ahead
  base price, both from the E-Control file). The price level starts at the previous month's base price and moves to
  the running month's base price so far (including tomorrow's known day-ahead prices), weighted by the share of the
  month whose prices are known. Configurable (`PriorMethod`); alternative: previous month as is. ("Previous month +
  last year's month-to-month change" was tested and removed: worst in every backtest.) Falls back to the previous month if data is missing. Until the previous month
  is published (2nd–7th), its PV value and base price are recomputed from Energy-Charts.
  See "Backtests of the estimate" below.
- **Official value wins.** Once E-Control publishes a month, it replaces the estimate for good.
- **Statistics:** hours in UTC, months assigned in `Europe/Vienna` (DST-safe); only the running month and the previous
  month (until official) are rewritten per update; `rebuild_statistics` and `set_official` services.
- **Python ≥ 3.14.2**, because that is what the current Home Assistant (2026.11) requires.

### Open points

- With live data the latest hour may have fewer than four quarter-hours — decide whether to drop incomplete hours.
- Once the package `__init__.py` imports Home Assistant, the pure tests need HA installed
  (pytest-homeassistant-custom-component) — acceptable, or move the pure code.

## Backtests of the estimate (2026-11-15)

### Monthly: the prior alone

Each month 2023-02..2026-10 (n = 45) predicted from earlier months only, compared with the official PV value
(ct/kWh; 2022 used as input only). `tests/test_calc.py::test_backtest_default_prior_beats_alternatives` pins it.

| Method | MAE | RMSE | max |
|---|---|---|---|
| previous month as is | 2.03 | 2.67 | 7.18 |
| same month last year | 6.79 | 10.80 | 40.29 |
| previous month + last year's month-to-month change | 4.25 | 6.39 | 23.46 |
| previous month × last year's month-to-month ratio | 2.83 | 4.02 | 13.52 |
| **previous base × last year's capture rate (default)** | 1.74 | **2.17** | **4.41** |
| previous base + last year's (PV − base) spread | **1.62** | 2.21 | 5.93 |

Same ranking for 2024+ and 2025+. Why: month-to-month changes are dominated by the general price level, which does not
repeat yearly (e.g. October: +0.44, +0.33, +4.01, +7.18 ct in 2023–2026). The **capture rate** is seasonal and drifts
slowly with PV build-out (April: 0.90, 0.82, 0.53, 0.32, 0.20 in 2022–2026), so the level comes from the latest month
and the seasonal PV discount from last year. Multiplicative, so it scales with the price level. Small sample and a
market in transition — the margins (0.3–0.5 ct) are indicative, not proof.

### Hourly: the whole estimate on day d (`tools/backtest_estimate.py`)

For each month 2023-02..2026-10 (n = 45) and day d, only what was known at midnight starting day d+1 is used:
generation up to 2 h before, day-ahead prices up to the end of the next day, the previous month's official value only
after its publication date (recomputed from Energy-Charts before). RMSE in ct/kWh against the official value:

| Method | d=1 | 3 | 5 | 7 | 10 | 15 | 20 | 25 |
|---|---|---|---|---|---|---|---|---|
| month so far only (no prior) | 6.03 | 3.66 | 2.76 | 2.19 | 1.75 | 1.55 | 1.12 | 0.66 |
| previous month | 2.70 | 2.65 | 2.54 | 2.39 | 2.18 | 1.83 | 1.39 | 0.87 |
| previous month + seasonal difference | 6.27 | 5.90 | 5.52 | 5.09 | 4.51 | 3.49 | 2.40 | 1.43 |
| base × capture rate, previous month's base only | 2.19 | 2.15 | 2.08 | 1.96 | 1.80 | 1.56 | 1.23 | 0.78 |
| **base × capture rate, level moving to the running month (default)** | **2.17** | **2.12** | **2.02** | **1.88** | **1.69** | **1.44** | **1.10** | **0.67** |

Data floor (whole month recomputed from Energy-Charts vs official): RMSE 0.20, max 1.29 (2024-06; APG corrections).
Findings: with only the previous month's base the prior drags late in the month — from day ~15 the month so far
alone is better. Moving the price level to the running month's known prices fixes that; the default is the best or
within 0.01 of the best on every day. Also tried: trusting the month so far more (square root of the PV share) —
better mid-month, worse early, not adopted.

## History

- 2026-11-15 — Milestone 1: `calc.py` + tests; reproduces the official Sep/Oct 2026 values from recorded
  Energy-Charts data.
- 2026-11-15 — MIT license; README installation section (HACS custom repository, My Home Assistant links) written
  ahead of the integration itself.
- 2026-11-15 — Switched all prices and the deduction default from ct/kWh to EUR/kWh (0.015 EUR/kWh).
- 2026-11-15 — Prior for the month estimate: backtested methods, default base × capture rate (`PriorMethod`).
- 2026-11-15 — Milestone 2: `sources.py` (Energy-Charts client, E-Control xlsx parser), tests against recorded
  responses and a local test server, opt-in live tests; ruff for lint/format.
- 2026-11-15 — Hourly backtest of the whole estimate; prior's price level now moves to the running month's base price.
- 2026-11-15 — Removed the seasonal-difference prior (worst method in both backtests); its backtest rows stay above.
