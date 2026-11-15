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
  `SourceDataError`. The test fixture is generated (see "Test data and licenses").

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

### Home Assistant integration (milestone 3)

- Built against Home Assistant 2026.10.4 — the newest version the test plugin (pytest-homeassistant-custom-component
  0.13.375) supports; `hacs.json` requires ≥ 2026.10.0. Patterns that only exist from 2026.11
  (`async_setup_entry` returning `None`, `async_unload_platforms_or_raise`) are not used; the classic forms work in
  both. Manifest keys follow hassfest's schema; `categories` (`energy.energy_prices`) is newer than 2026.10, whose loader
  ignores it — the Marketplace validation warns without it.
- Modelled on the core **Nord Pool** integration (a platinum price integration): typed `runtime_data`, coordinator
  with `config_entry=`, `_async_setup` for one-time work (loading the time zone file asynchronously), entity
  descriptions with `value_fn`, `has_entity_name` + translation keys, a service device, `PARALLEL_UPDATES = 0`.
- **Estimate orchestration is pure** (`estimate.py`): the coordinator only fetches and caches. Only *complete* hours
  of generation are used (a partial latest hour would get an incomplete mean); prices are known until the last
  returned quarter-hour (Energy-Charts truncates future requests, no nulls).
- **Request budget**: see "Download schedule" below (replaced the first version, which re-downloaded the whole
  month every hour).
- **E-Control outage does not take the estimate down**: the last table is kept, logged once when it starts failing
  and once when it recovers; without any table the estimate uses the month so far and the official sensors are
  unknown.
- **Settings split**: `entry.data` = data sources (setup section, *Reconfigure*; re-validated against the real sources,
  unique ID = country + bidding zone + generation type + E-Control column, so several series can coexist);
  `entry.options` = deduction, method, aggregation, intervals (*Configure*, `OptionsFlowWithReload`). Invalid
  settings are told apart from connection problems by the HTTP status (Energy-Charts answers 400 for an unknown
  zone/country).
- Price sensors: `EUR/kWh`, state class measurement, no device class (monetary only allows `total`); the month table
  attribute is excluded from the recorder. Diagnostics redact the optional header values and show the URLs without
  query and credentials (`sources.display_url`, also used in every error message, which ends up in the log).
- *Reconfigure* changes the entry's title along with the data series only while it is still the automatic one; a
  title the user set is kept.
- Brand image in `custom_components/referenzmarktwert/brand/` (supported since HA 2026.3), drawn by
  `tools/make_icon.py`.
- hassfest could not be run locally (it executes HA core's scripts); it runs in CI (GitHub Actions: hassfest,
  `hacs/action` with category integration, ruff + pytest; dependabot keeps the actions current).
- **Schemas use `probatio`**, not `voluptuous`: Home Assistant 2026.10 replaced voluptuous (all core flows import
  probatio, `homeassistant` only requires `probatio`; voluptuous is merely pulled in by test tools). A test runs all
  three forms through the serializer of HA's flow HTTP views (`probatio.to_field_list`).
- Energy-Charts answers **HTTP 404 "no content available"** for a window without data (e.g. the first hours of a
  month): treated as an empty series; any other 404 (wrong path) stays an error. The setup validation therefore asks
  for windows that certainly hold data (generation 2 days ago, prices of the last day).
- A failed E-Control download is retried every daytime interval until it works (one failure at startup must not hide
  the official values for a day).

### Download schedule and stored data

What is due when is decided by the pure `schedule.plan(now, tz, sun, settings, status)`, which also returns when the
next thing becomes due; the coordinator sets its `update_interval` to that after every run (so it also wakes at night
for prices, E-Control, the month change — and a long daytime interval cannot starve the 30-min price retry).

- **Generation only in the daylight window** (sunrise + 60 min .. sunset + 180 min; offsets are options). PV
  generation outside it is zero, or a few MW at dawn and dusk (Oct 2026: 128 quarter-hours of 4–8 MW, 0.045 % of the
  month's energy) — they are not lost: every download starts at the **last stored point minus an overlap** (3 h),
  so the morning's first download fetches them. In between, the estimate lacks them: measured for October 2026 at
  the month end, 0.0015 ct/kWh (one measurement, not a bound).
  Sunrise/sunset come from Home Assistant's home location (`helpers.sun`, computed locally); a window past midnight
  (summer) is handled by also looking at yesterday's window; without sun times (polar) there is no gating.
- **One full download per night** (02:00, option) of the running month — plus the previous month while it is not
  official — as a safety net in case Energy-Charts revises older values (not observed yet; not verifiable from one
  day's recordings).
- **Prices once a day**: day-ahead results are published ~12:45–13:00 CET for the whole next day. Fetched when today's
  are missing (at once), and from 13:30 (option) every 30 min until tomorrow's are there. Prices are stored by time
  stamp across month ends, so the 1st's prices arrive with the last day's download (the first version capped requests
  at the month end and would have had no prices for the 1st until 13:30).
- **E-Control only if changed**: the server sends `Last-Modified` (no `ETag`) and answers `If-Modified-Since` with
  `304` and no body (verified 2026-11-15). The conditional header is only sent while the parsed table for that date is
  held (otherwise a restart would get a 304 and no table). Early checks run until day 15 (option): publication days
  2022-01..2026-10 were 2–14 (mostly 2–9; day 14 once, Jan 2023; day 10 twice) — the first version's "until the
  10th" would have missed Jan 2023. The file's Last-Modified (5 Nov 2026) can be a day after the publication date in
  the sheet (4 Nov).
- **Stored data** (`cache.py`, saved with `helpers.storage.Store` as `.storage/referenzmarktwert.<entry_id>`): the
  generation and price series from the start of the previous month, the reference month, the official table with its
  Last-Modified date, and when what was last tried. A restart or reload therefore costs no downloads, and the previous
  month needs no extra download (it was the running month). A **fingerprint** of the data-source settings is stored
  with it: after a *Reconfigure* to other sources the data is discarded. The file is deleted with the entry
  (`async_remove_entry`, which also deletes the repair notice). Diagnostics show counts, never sunrise/sunset-based
  times (they would reveal the location). **Unreadable data is downloaded again, never a reason to fail**: a file of
  another format version (older, or newer after a downgrade) loads as empty (`CacheStore`: no migration code for
  data that can be fetched anew), unexpected content too (`CacheState.from_dict`); a damaged JSON file is renamed to
  `*.corrupt.<time>` by Home Assistant itself. After an unload an update still running does not save any more (it
  would recreate the file of a removed entry).
- **Failures**: an Energy-Charts outage keeps the stored data and the sensors (logged once when it starts and once
  when it recovers); only if nothing was ever downloaded is the update failed (setup is retried) — also when a
  retry is still blocked by a rate limit. HTTP 429 stores a "not before" time from `Retry-After`, limited to one day
  (an endless value would be stored and block for good); E-Control is not blocked by it. The pause between requests
  is only made between them, not after the last. Without an official table (e.g. the download
  failed before a restart) E-Control counts as failing and is retried every daytime interval.
- The step of a series is taken from the data (`calc.resolution`), not assumed to be 15 min: with hourly prices the
  coverage would otherwise look incomplete and the prices would be fetched every 30 min.
- Energy-Charts has had `/v2/...` twins of every endpoint since 2026-07-20; v1 "remains unchanged", and neither
  version can return a single production type — so `public_power` stays on v1 and always carries all types. v2
  answers are ~3× larger (one object per quarter-hour with ISO time stamps and series names). Energy-Charts announces
  the end of an endpoint at least six months ahead with `"deprecated": true` in every answer: the integration then
  raises a repair issue (`energy_charts_deprecated`), so there is time to switch.

### Test data and licenses

- Energy-Charts API data is CC BY 4.0 with attribution to Energy-Charts.info (API description, "Data License"); the AT
  day-ahead prices are CC BY 4.0 from Bundesnetzagentur | SMARD.de (stated in the `price` response's
  `license_info`). The recorded responses in `tests/fixtures/` may therefore be shared, with attribution (README).
- E-Control states no open license (only "© E-Control"). The repository therefore no longer contains E-Control's
  file: `tools/make_econtrol_fixture.py` generates an xlsx with the same layout (title, shared-string headers, unit
  row, Excel serial dates, source line) holding the published monthly values, reproducibly. Values match the original
  to 5·10⁻¹² Cent/kWh. The earlier copy remains in the git history (not rewritten, by decision).

### Open points

- Once the package `__init__.py` imports Home Assistant, the pure tests need HA installed — accepted: the test plugin
  installs it anyway.

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
- 2026-11-15 — HACS is now the built-in **Marketplace** (HA 2026.11, core component `marketplace`, catalog
  `hacs/default`, same `hacs.json` + `custom_components/<domain>/` format, My link `marketplace_repository`; the
  old `hacs_repository` link redirects). README installation rewritten. Tests now run on Home Assistant's test plugin
  (pytest-homeassistant-custom-component, newest release pins HA 2026.10.4), which blocks sockets: the local test
  server uses `socket_enabled`, the live tests an `internet` fixture.
- 2026-11-15 — Milestone 3: Home Assistant integration (flows, coordinator, sensors, diagnostics, brand icon).
- 2026-11-15 — Download schedule: generation only in the daylight window and incremental (+ nightly full download),
  prices once a day, E-Control only if changed (`If-Modified-Since`), downloads stored across restarts, new options
  (window offsets, overlap, full-download time, price time and retry, early-check days, E-Control header); README
  lists every request and the data licenses; the E-Control fixture is generated instead of copied.
- 2026-11-15 — CI workflows (hassfest, Marketplace/HACS validation, lint, tests); repair notice when Energy-Charts
  marks its endpoints as deprecated; repository topics set.
- 2026-11-15 — Code review before the first release: Reconfigure keeps a user-set title; `Retry-After` limited to a
  day; unreadable stored data (damaged, other format version) is downloaded again instead of failing setup; the repair
  notice is deleted with the entry; a rate-limited retry no longer starts with empty sensors; no save after unload;
  URLs without query/credentials in errors and diagnostics; duplicates removed (`calc.covered_until` derives the step,
  `calc.clamp01`, `const.entry_data`/`entry_options`, one test fake for all tests); `Series` is a `Sequence`;
  coordinator data compared before notifying the sensors (`always_update=False`).
