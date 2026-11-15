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
- **Units.** Internally EUR/MWh (as delivered) and MW; sensors will report EUR/kWh, the unit the Energy Dashboard needs.
- **Whole-month estimate** = month so far, blended with the previous month's official value; the weight is the share
  of the month's *expected PV energy* already passed (reference: the same month last year). Weighted by PV energy, not
  days. If the share is unknown (reference data missing) the month so far is used as is — real data is never
  discarded. The elapsed time is taken from the latest data point, not the wall clock (generation lags ~2 h).
- **Official value wins.** Once E-Control publishes a month, it replaces the estimate for good.
- **Statistics:** hours in UTC, months assigned in `Europe/Vienna` (DST-safe); only the running month and the previous
  month (until official) are rewritten per update; `rebuild_statistics` and `set_official` services.
- **Python ≥ 3.14.2**, because that is what the current Home Assistant (2026.11) requires.

### Open points

- With live data the latest hour may have fewer than four quarter-hours — decide whether to drop incomplete hours.
- Once the package `__init__.py` imports Home Assistant, the pure tests need HA installed
  (pytest-homeassistant-custom-component) — acceptable, or move the pure code.

## History

- 2026-11-15 — Milestone 1: `calc.py` + tests; reproduces the official Sep/Oct 2026 values from recorded
  Energy-Charts data.
- 2026-11-15 — MIT license; README installation section (HACS custom repository, My Home Assistant links) written
  ahead of the integration itself.
