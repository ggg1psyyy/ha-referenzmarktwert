# Referenzmarktwert PV — Home Assistant integration

> **Status: early development — not installable yet.**
> Only the calculation core exists. There is no Home Assistant integration (config flow, sensors, statistics) yet.

A custom [Home Assistant](https://www.home-assistant.io/) integration (to be installed via HACS) that computes the
Austrian **reference market value** (*Referenzmarktwert*, § 13 EAG) of the running month from public data, and values
grid export with it for the Energy Dashboard. Many Austrian feed-in tariffs are defined as
"Referenzmarktwert Photovoltaik − deduction".

## Installation

> Not functional yet — the integration part does not exist so far. The steps below describe how it will be installed.

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

## Developed with Claude

This project is developed with the help of **Claude** (Anthropic's AI assistant, via Claude Code): code, tests and
documentation are written by Claude and reviewed by the maintainer. Commits are made by the maintainer.

## Current state

| Part | State |
|---|---|
| Calculation (`calc.py`): hourly aggregation, generation-weighted mean, month estimate (prior + blend), payout | done, tested |
| Data sources (`sources.py`): Energy-Charts client, E-Control history file parser | done, tested |
| HA integration: config flow, coordinator, sensors | not started |
| Energy Dashboard compensation statistic, backfill, services | not started |
| HACS release, CI | not started |

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

All parameters (aggregation period, deduction, prior method, data source URLs, country, bidding zone, generation type,
time zone) have defaults in `custom_components/referenzmarktwert/const.py` and are passed in as parameters, so they can
be made user-configurable.

## Development

Requires Python ≥ 3.14.2 (the current Home Assistant minimum). With [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv pytest-homeassistant-custom-component==0.13.375 ruff
.venv/bin/python -m pytest                       # offline tests (recorded data, local test server)
RMW_LIVE=1 .venv/bin/python -m pytest -m live    # optional: against the real APIs
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for design decisions and their reasons.

## Data sources and attribution

- **Energy-Charts** (Fraunhofer ISE), https://energy-charts.info — generation and day-ahead price data, licensed
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (price data originally from Bundesnetzagentur | SMARD.de).
  `tests/fixtures/public_power_*.json` and `price_*.json` are recorded Energy-Charts responses.
- **E-Control**, https://www.e-control.at/referenzmarktwert — official monthly reference market values.
  `tests/fixtures/Referenzmarktwert_Entwicklung.xlsx` is a copy of E-Control's published history file.

This project is not affiliated with Fraunhofer ISE, E-Control or any electricity supplier.

## License

[MIT](LICENSE)
