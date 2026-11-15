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
- **E-Control** history file `Referenzmarktwert_Entwicklung.xlsx` (monthly since 2022-01; columns: month, hydro,
  wind, PV, base price, publication date). Parsed with the standard library (it is a zip of XML) — no openpyxl.

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
- **Prior = previous month's base price × last year's capture rate of this calendar month** (capture rate = PV value /
  day-ahead base price, both from the E-Control file). Configurable (`PriorMethod`); alternatives: previous month as
  is, previous month + last year's month-to-month change. Falls back to the previous month if data is missing.
  See "Backtest of the prior" below.
- **Official value wins.** Once E-Control publishes a month, it replaces the estimate for good.
- **Statistics:** hours in UTC, months assigned in `Europe/Vienna` (DST-safe); only the running month and the previous
  month (until official) are rewritten per update; `rebuild_statistics` and `set_official` services.
- **Python ≥ 3.14.2**, because that is what the current Home Assistant (2026.11) requires.

### Open points

- With live data the latest hour may have fewer than four quarter-hours — decide whether to drop incomplete hours.
- Once the package `__init__.py` imports Home Assistant, the pure tests need HA installed
  (pytest-homeassistant-custom-component) — acceptable, or move the pure code.

## Backtest of the prior (2026-11-15)

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

To test once hourly data is available (milestone 2): use the *running* month's base price so far (day-ahead known up
to tomorrow) instead of the previous month's. The prior matters mainly in days 1–10, before the previous month's
official value is published; until then the previous month (PV and base) is recomputed from Energy-Charts.

## History

- 2026-11-15 — Milestone 1: `calc.py` + tests; reproduces the official Sep/Oct 2026 values from recorded
  Energy-Charts data.
- 2026-11-15 — MIT license; README installation section (HACS custom repository, My Home Assistant links) written
  ahead of the integration itself.
- 2026-11-15 — Switched all prices and the deduction default from ct/kWh to EUR/kWh (0.015 EUR/kWh).
- 2026-11-15 — Prior for the month estimate: backtested methods, default base × capture rate (`PriorMethod`).
