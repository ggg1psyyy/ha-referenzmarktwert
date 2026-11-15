# Referenzmarktwert PV — Home Assistant integration

> **Status: early development — not installable yet.**
> Only the calculation core exists. There is no Home Assistant integration (config flow, sensors, statistics) yet.

A custom [Home Assistant](https://www.home-assistant.io/) integration (to be installed via HACS) that computes the
Austrian **reference market value** (*Referenzmarktwert*, § 13 EAG) of the running month from public data, and values
grid export with it for the Energy Dashboard. Many Austrian feed-in tariffs are defined as
"Referenzmarktwert Photovoltaik − deduction".

## Installation

> Not functional yet — the integration part does not exist so far. The steps below describe how it will be installed.

### Via HACS (recommended)

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ggg1psyyy&repository=ha-referenzmarktwert&category=integration)

Or manually in HACS:

1. Open **HACS** in Home Assistant.
2. Open the menu (⋮, top right) → **Custom repositories**.
3. Repository: `https://github.com/ggg1psyyy/ha-referenzmarktwert`, type **Integration** → **Add**.
4. Search for **Referenzmarktwert PV**, open it and click **Download**.
5. **Restart** Home Assistant.

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
| Data sources (Energy-Charts client, E-Control history file) | not started |
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
energy already passed. Default prior: the previous month's day-ahead base price × the PV/base ratio ("capture rate")
of the same calendar month last year. Alternatives: the previous month's value as is, or the previous month plus last
year's month-to-month change.

All parameters (aggregation period, deduction, prior method, data source URLs, country, bidding zone, generation type,
time zone) have defaults in `custom_components/referenzmarktwert/const.py` and are passed in as parameters, so they can
be made user-configurable.

## Development

Requires Python ≥ 3.14.2 (the current Home Assistant minimum). With [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv pytest
.venv/bin/python -m pytest
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for design decisions and their reasons.

## Data sources and attribution

- **Energy-Charts** (Fraunhofer ISE), https://energy-charts.info — generation and day-ahead price data, licensed
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (price data originally from Bundesnetzagentur | SMARD.de).
  `tests/fixtures/public_power_*.json` and `price_*.json` are recorded Energy-Charts responses.
- **E-Control**, https://www.e-control.at/referenzmarktwert — official monthly reference market values.
  `tests/fixtures/econtrol_monthly.json` is an extract of E-Control's published history (PV and base price).

This project is not affiliated with Fraunhofer ISE, E-Control or any electricity supplier.

## License

[MIT](LICENSE)
