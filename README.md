# Referenzmarktwert PV — Home Assistant integration

> **Status: alpha — 0.1.3.**
> The sensors work; the Energy Dashboard compensation statistic is not implemented yet.

A custom [Home Assistant](https://www.home-assistant.io/) integration (installed through the Marketplace) that computes the
Austrian **reference market value** (*Referenzmarktwert*, § 13 EAG) of the running month from public data, and values
grid export with it for the Energy Dashboard. Many Austrian feed-in tariffs are defined as
"Referenzmarktwert Photovoltaik − deduction".

## Installation


### Via the Marketplace (recommended, Home Assistant 2026.11+)

Since Home Assistant 2026.11, custom integrations are installed through the built-in **Marketplace** (the successor of
HACS; this repository uses the same `hacs.json` format, so it also works with a legacy HACS installation).

[![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=ggg1psyyy&repository=ha-referenzmarktwert&category=integration)

Or step by step:

1. Open the **Marketplace** in Home Assistant. Adding a custom repository needs a **GitHub connection** in the
   Marketplace (read-only, public information only) — the Marketplace asks for it if it is missing.
2. Open the custom repositories
   ([![Open the Marketplace custom repositories.](https://my.home-assistant.io/badges/marketplace_repositories.svg)](https://my.home-assistant.io/redirect/marketplace_repositories/))
   and add `https://github.com/ggg1psyyy/ha-referenzmarktwert` with the category **Integration**.
3. Open **Referenzmarktwert PV** in the Marketplace and install it.
4. **Restart** Home Assistant when asked.

### Manual

Copy `custom_components/referenzmarktwert/` from the latest release into the `custom_components/` folder of your
Home Assistant configuration directory, then restart Home Assistant.

### Setup

[![Open your Home Assistant instance and start setting up the integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=referenzmarktwert)

Or: **Settings** → **Devices & services** → **Add integration** → search for **Referenzmarktwert PV**.

The setup asks only for the **deduction** (EUR/kWh, subtracted for the payout sensors; Hofer "Sonne": 0.015). All data
sources are preset for Austrian photovoltaics and sit in a collapsed **Data sources** section; every setting is tried
against the real sources before it is saved.

## Sensors

Entity IDs are English in every language (e.g. `sensor.referenzmarktwert_pv_official_value_latest`); only the
displayed names are translated. Home Assistant itself would derive the IDs from the translated names. Entities
created by an older version keep their IDs; to switch, open the device and choose **Menu → Recreate entity IDs**.

All values in **EUR/kWh**. Attribute names start with their group (`calc_`, `data_until_`, `previous_month_`,
`range_`, …), because Home Assistant lists attributes sorted by name.

| Sensor | Value | Main attributes |
|---|---|---|
| Estimate running month | expected reference market value of the running month | `calc_known_share`, `calc_method`, `calc_month_so_far`, `calc_prior`, `calc_running_base_price`; `data_until_generation`, `data_until_prices`; `month`; `previous_month_source` (official/recomputed), `previous_month_value`; `range_error`, `range_high`, `range_low` (estimate ± expected error × range width) |
| Payout estimate running month | estimate − deduction (the expected feed-in price) | `deduction`, `month`, `range_error`, `range_high`, `range_low` |
| Expected error of the estimate | how far off the estimate can be (one standard deviation, see below) | `month` |
| Estimate running month low / high, Payout estimate running month low / high *(disabled by default)* | the bounds of the range, the same values as `range_low` / `range_high`, e.g. for a cautious calculation with the low value. Enable them in the entity's settings | `month`, `range_error` (payout: also `deduction`) |
| Official value latest | the latest month published by E-Control (normally last month; until it is published, the month before) | `file_checked`, `history` (all months; not stored in the database), `month`, `month_base_price`, `month_published` |
| Payout official value latest | official value − deduction | `month`, `deduction` |

## Settings

| Where | Setting | Default |
|---|---|---|
| Setup, **Configure** | Deduction | 0.015 EUR/kWh |
| **Configure** | Estimate for the rest of the month | base price × last year's capture rate |
| | Aggregation period | 3600 s (as E-Control) |
| | Expected error at the start / end of the month | 0.0225 / 0.0025 EUR/kWh |
| | Range width (range = estimate ± this many expected errors) | 1 |
| | Interval while PV produces (generation) | 60 min |
| | Start after the first / end after the last generation (production window, from yesterday's data) | 120 min / 150 min |
| | Overlap (each download repeats the last hours) | 3 h |
| | Nightly full download of the running month | 02:00 |
| | Tomorrow's prices from / retry interval | 13:30 / 30 min |
| | Official values: check interval / while awaited / awaited until day | 24 h / 3 h / 15 |
| | Pause between requests | 1 s |
| | Random spread of the planned download times (new every day; 0 = off) | 10 min |
| Setup section *Data sources*, **Reconfigure** | Energy-Charts API URL, generation and price paths | `https://api.energy-charts.info`, `/public_power`, `/price` |
| | Country, bidding zone, generation type | `at`, `AT`, `Solar` |
| | Extra HTTP header for Energy-Charts (name, value — e.g. an API key) | none |
| | E-Control file URL; extra HTTP header for E-Control | E-Control's `Referenzmarktwert_Entwicklung.xlsx`; none |
| | E-Control columns: month, value, base price, publication date | A, D, E, F |
| | Time zone (months are counted in it), request timeout | Europe/Vienna, 30 s |

## Developed with Claude

This project is developed with the help of **Claude** (Anthropic's AI assistant, via Claude Code): code, tests and
documentation are written by Claude and reviewed by the maintainer. Commits are made by the maintainer.

## Current state

| Part | State |
|---|---|
| Calculation (`calc.py`): hourly aggregation, generation-weighted mean, month estimate (prior + blend), payout | done, tested |
| Data sources (`sources.py`): Energy-Charts client, E-Control history file parser | done, tested |
| HA integration: setup/reconfigure/options flows, coordinator, sensors, diagnostics | done, tested |
| Download schedule (`schedule.py`: production window from the data, daily prices, conditional E-Control check) and stored data (`cache.py`) | done, tested |
| Energy Dashboard compensation statistic, backfill, services | not started |
| CI: hassfest, Marketplace (HACS) validation, lint and tests on every push (`.github/workflows/`) | done |
| Releases | 0.1.0 (pre-release), 0.1.1, 0.1.2, 0.1.3 |

### How the value is calculated

```
RMW = Σ_h (price_h × generation_h) / Σ_h generation_h
```

per hour `h` of the month: `price_h` is the mean of the hour's quarter-hourly day-ahead prices (bidding zone AT),
`generation_h` the hour's PV generation in the Austrian control area. Negative prices count normally. With the recorded
data for September and October 2026 this reproduces the official E-Control values (10.53 and 17.71 ct/kWh).

All prices are in **EUR/kWh** (E-Control publishes ct/kWh; 17.71 ct/kWh = 0.1771 EUR/kWh), because Home Assistant's
Energy Dashboard books prices in the main currency unit.

### Estimate of the running month

The month so far is blended with a prior for the rest of the month, weighted by the share of the month's expected PV
energy already passed. Default prior: the day-ahead base price level (the previous month's, moving to the running
month's as its prices become known) × the PV/base ratio ("capture rate") of the same calendar month last year.
Backtested on 2023–2026: about ±2 ct/kWh (RMSE) on the 1st of the month, ±1.7 on the 10th, ±1.1 on the 20th
(`tools/backtest_estimate.py`). Alternative: the previous month's value as is.

Test of the whole integration (default settings), day by day on recorded data of 16 random months, 4 per season
(2023–2026; `tests/test_backtest.py` with seed 15809). Data was served as it is published (generation 2 h late, tomorrow's prices from 13:00, E-Control on its
real publication day). The table shows the error of the estimate against the official value, ct/kWh, at the end of
the day: mean of the absolute errors (largest in brackets).

| Season (months) | Day 1 | Day 10 | Day 20 | Day 25 | Last day | Below 0.25 from day | Below 0.10 from day |
|---|---|---|---|---|---|---|---|
| Winter (2023-02, 2023-12, 2025-12, 2026-02) | 2.23 (4.25) | 2.44 (4.19) | 1.92 (3.30) | 1.28 (2.57) | 0.04 | 25–30 | 26–31 |
| Spring (2024-03, 2024-05, 2025-03, 2026-04) | 1.85 (3.71) | 1.20 (1.98) | 1.16 (1.80) | 0.65 (0.97) | 0.07 | 26–31 | 31 |
| Summer (2023-06, 2023-08, 2024-08, 2025-07) | 0.58 (1.17) | 0.66 (0.81) | 0.45 (1.13) | 0.24 (0.40) | 0.02 | 24–28 | 26–29 |
| Autumn (2023-11, 2024-11, 2025-09, 2025-10) | 1.62 (3.72) | 1.34 (1.98) | 0.33 (0.86) | 0.25 (0.66) | 0.02 | 15–29 | 28–30 |
| All 16 | 1.57 (4.25) | 1.41 (4.19) | 0.97 (3.30) | 0.60 (2.57) | 0.04 | 15–31 | 26–31 |

On the last day the error is only the difference between Energy-Charts' data and E-Control's own computation
(at most 0.14 ct/kWh); in three months (2024-05, 2026-02, 2026-04) it stays just above 0.10 for that reason.

**Expected error.** The estimate comes with how far off it can be: 2.25 ct/kWh × (1 − `calc_known_share`) + 0.25 ct/kWh
(both values are options), shown as its own sensor. The range `range_low`/`range_high` (and the low/high sensors) is the
estimate ± `range_error` = expected error × range width (option, default 1; 2 for about 93 %). Fitted on all days of
2023-02..2026-10, each year left out in turn: 74 % of the real errors were within ± the expected error, 93 % within
twice it (days 1–10: 71 %, 11–20: 72 %, 21–31: 79 %). It is the typical error, not a limit: in a month whose prices
move unusually (December 2023, February 2026) the estimate can be off by twice as much for weeks.

## Development

Requires Python ≥ 3.14.2 (the current Home Assistant minimum). With [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv pytest-homeassistant-custom-component==0.13.375 ruff
.venv/bin/python -m pytest                       # offline tests (recorded data, local test server)
RMW_LIVE=1 .venv/bin/python -m pytest -m live    # optional: against the real APIs
# optional, slow: the whole integration day by day on recorded months (data downloaded once by the tool)
.venv/bin/python tools/backtest_estimate.py --cache /some/dir
RMW_BACKTEST=/some/dir RMW_BACKTEST_SEED=15809 .venv/bin/python -m pytest -m backtest -s
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

GitHub Actions run hassfest, the Marketplace (HACS) validation, lint and the offline tests on every push.

See [ARCHITECTURE.md](ARCHITECTURE.md) for design decisions and their reasons.

## Downloads

Everything is downloaded by your Home Assistant directly from the two sources; nothing is sent anywhere else. The
requests carry only their parameters and Home Assistant's standard User-Agent (Home Assistant, aiohttp and Python
versions), plus the optional extra headers you configure. Home Assistant's home location is not used: the download
times follow the national generation data, so they reveal nothing about where you live. The planned times (window
start and end, the nightly full download, the price time, last year's month after a month change) move by a random
amount of up to ±5 min, new every day, so that not every installation asks at the same second. Times below are local (Europe/Vienna), with the default
settings. `start`/`end` are Unix seconds.

| What | Request | When |
|---|---|---|
| PV generation of the running month (Energy-Charts answers with every generation type; only `Solar` is used) | `https://api.energy-charts.info/public_power?country=at&start=…&end=…` | in the **production window** (yesterday's first generation + 2 h until its last generation + 2.5 h, e.g. 07:45–22:15 in mid-October) every 60 min, only the hours since the last stored point (minus a 3 h overlap); once more after the window; **once a night** (02:00) the whole running month |
| Day-ahead prices (EUR/MWh) | `https://api.energy-charts.info/price?bzn=AT&start=…&end=…` | **once a day** after the auction (from 13:30, again every 30 min until tomorrow's prices are there); at once if today's are missing |
| PV generation of the same month last year (for the expected generation share) | `https://api.energy-charts.info/public_power?country=at&start=…&end=…` | once a month (0–10 min after midnight on the 1st) |
| Official values | `https://www.e-control.at/documents/1785851/10823410/Referenzmarktwert_Entwicklung.xlsx` | checked daily, every 3 h until the 15th while last month's value is awaited; transferred **only if it changed** (`If-Modified-Since`; otherwise the server answers `304 Not Modified` without content) |

If Energy-Charts ever marks these endpoints as deprecated (it announces that at least six months ahead in every
answer), the integration shows a repair notice in Home Assistant.

Generation outside the production window is zero (Energy-Charts reports exactly 0 at night); the few small values
of a day that produces longer than the day before arrive with the next download, so nothing is lost. Downloads are stored in Home Assistant's
`.storage` folder, so a restart or reload costs no downloads; the previous month needs no extra download either (it was
the running month). If that file cannot be read (damaged, or from another version of the integration), the data is
simply downloaded again; deleting the entry deletes the file. The connection test in setup and reconfigure makes three small requests: generation of one day,
prices of one day, and the E-Control file. Per day (simulated with the recorded data and the default settings): 16 generation downloads on 15 October, 18 on
15 September, plus 1–2 price downloads and one E-Control check; around June it will be more (longer days, not
measured yet).

## Data sources and licenses

- **Energy-Charts** ([Fraunhofer ISE](https://energy-charts.info), [API](https://api.energy-charts.info/)) — PV
  generation and day-ahead prices. The API data is licensed under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) with attribution to Energy-Charts.info
  ([publishing notes](https://energy-charts.info/publishing-notes.html)); the day-ahead prices for AT are CC BY 4.0 from
  [Bundesnetzagentur | SMARD.de](https://www.smard.de/), published unchanged. `tests/fixtures/public_power_*.json` and
  `price_*.json` are recorded Energy-Charts responses (reduced to the `Solar` series), shared under the same license.
- **E-Control** ([Referenzmarktwert](https://www.e-control.at/referenzmarktwert)) — the official monthly reference
  market values, © E-Control. The integration downloads E-Control's published file at runtime; this repository does not
  contain it. `tests/fixtures/Referenzmarktwert_Entwicklung.xlsx` is generated by `tools/make_econtrol_fixture.py` and
  holds the published monthly values (source: E-Control) in the same layout, for the tests.

This project is not affiliated with Fraunhofer ISE, E-Control, Bundesnetzagentur or any electricity supplier.

## License

[MIT](LICENSE)
