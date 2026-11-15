"""Constants and defaults for the Referenzmarktwert integration.

Everything here is a *default*: the config/options flow lets the user override each value.
This module must stay free of Home Assistant imports so the pure modules (calc, sources) can use it.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

DOMAIN: Final = "referenzmarktwert"

# --- Calculation -------------------------------------------------------------------------------
# E-Control weights *hourly* values (§ 13 EAG), even though the source data is quarter-hourly.
DEFAULT_AGGREGATION_SECONDS: Final = 3600

# Feed-in price = reference market value - deduction (Hofer "Sonne": 1.5 ct/kWh, CPI-indexed).
# All prices are in the main currency unit (EUR), as Home Assistant's Energy Dashboard requires.
DEFAULT_DEDUCTION_EUR_PER_KWH: Final = 0.015


class PriorMethod(StrEnum):
    """How the not-yet-known rest of the month is estimated (the "prior" of the blend).

    Backtests (see ARCHITECTURE.md): monthly history 2023-02..2026-10, RMSE ct/kWh:
    BASE_X_CAPTURE_RATE 2.17, PREVIOUS_MONTH 2.67. Hourly, whole estimate on day 1/10/20:
    2.17/1.69/1.10 vs 2.70/2.18/1.39.
    """

    # price level (previous month's base, moving to the running month's) x the same calendar
    # month's PV/base ratio (capture rate) last year
    BASE_X_CAPTURE_RATE = "base_x_capture_rate"
    # previous month's PV value as is
    PREVIOUS_MONTH = "previous_month"


DEFAULT_PRIOR_METHOD: Final = PriorMethod.BASE_X_CAPTURE_RATE

# --- Energy-Charts (Fraunhofer ISE), data licensed CC BY 4.0 ---------------------------------
DEFAULT_ENERGY_CHARTS_BASE_URL: Final = "https://api.energy-charts.info"
DEFAULT_COUNTRY: Final = "at"
DEFAULT_BIDDING_ZONE: Final = "AT"
DEFAULT_PRODUCTION_TYPE: Final = "Solar"
DEFAULT_ENERGY_CHARTS_GENERATION_PATH: Final = "/public_power"
DEFAULT_ENERGY_CHARTS_PRICE_PATH: Final = "/price"

# --- E-Control official values ------------------------------------------------------------------
DEFAULT_ECONTROL_XLSX_URL: Final = (
    "https://www.e-control.at/documents/1785851/10823410/Referenzmarktwert_Entwicklung.xlsx"
)
DEFAULT_ECONTROL_MONTH_COLUMN: Final = "A"  # month as Excel date
DEFAULT_ECONTROL_COLUMN: Final = "D"  # B hydro, C wind, D photovoltaics
DEFAULT_ECONTROL_BASE_COLUMN: Final = "E"  # Referenzmarktpreis (day-ahead base), for the capture rate
DEFAULT_ECONTROL_PUBLISHED_COLUMN: Final = "F"  # publication date as Excel date

# --- HTTP --------------------------------------------------------------------------------------
DEFAULT_REQUEST_TIMEOUT_SECONDS: Final = 30.0
# A server's "Retry-After" is obeyed up to this long; a larger (or endless) value would block for good.
MAX_RETRY_AFTER_SECONDS: Final = 86400.0

# --- Data ----------------------------------------------------------------------------------------
QUARTER_HOUR: Final = 900  # resolution of the Energy-Charts series (assumed if a series has < 2 points)

# --- Time --------------------------------------------------------------------------------------
DEFAULT_TIME_ZONE: Final = "Europe/Vienna"  # months are assigned in this zone

# --- Units ---------------------------------------------------------------------------------------
# Exchange prices arrive in EUR/MWh; sensors and statistics use EUR/kWh.
KWH_PER_MWH: Final = 1000.0

# --- Polling -------------------------------------------------------------------------------------
# Generation is fetched only while the sun can produce: from sunrise + offset until sunset + offset
# (Energy-Charts lags ~2 h, so the window ends a few hours after sunset). Each download asks only
# for the hours since the last stored point (minus an overlap for late corrections); one full
# download of the running month per night catches anything else.
DEFAULT_UPDATE_INTERVAL_MINUTES: Final = 60  # generation during the daylight window
DEFAULT_SUNRISE_OFFSET_MINUTES: Final = 60  # window starts this long after sunrise
DEFAULT_SUNSET_OFFSET_MINUTES: Final = 180  # ... and ends this long after sunset
DEFAULT_OVERLAP_HOURS: Final = 3  # each download repeats the last hours (late or corrected values)
DEFAULT_FULL_REFRESH_TIME: Final = "02:00:00"  # nightly full download of the running month (local time)
# Day-ahead prices are published once a day (auction ~12:00, results ~12:45-13:00 CET).
DEFAULT_PRICE_TIME: Final = "13:30:00"  # look for tomorrow's prices from this local time ...
DEFAULT_PRICE_RETRY_MINUTES: Final = 30  # ... and again this often until they are there
# E-Control: published on day 2-14 of the following month (2022-01..2026-10, mostly 2-9).
DEFAULT_OFFICIAL_INTERVAL_HOURS: Final = 24  # E-Control file, normally (only downloaded if changed)
DEFAULT_OFFICIAL_EARLY_INTERVAL_HOURS: Final = 3  # ... while last month's value is awaited
DEFAULT_OFFICIAL_EARLY_DAYS: Final = 15  # "awaited": until this day of the month
DEFAULT_REQUEST_DELAY_SECONDS: Final = 1.0  # pause between requests (rate limit)
PRICE_LOOKAHEAD_SECONDS: Final = 2 * 86400  # prices are asked until now + 2 days; the API returns what exists

# --- Config entry keys ---------------------------------------------------------------------------
# entry.data: where the data comes from (changed with "Reconfigure")
CONF_ENERGY_CHARTS_URL: Final = "energy_charts_url"
CONF_GENERATION_PATH: Final = "generation_path"
CONF_PRICE_PATH: Final = "price_path"
CONF_COUNTRY: Final = "country"
CONF_BIDDING_ZONE: Final = "bidding_zone"
CONF_PRODUCTION_TYPE: Final = "production_type"
CONF_HEADER_NAME: Final = "header_name"  # optional extra HTTP header for Energy-Charts (e.g. an API key)
CONF_HEADER_VALUE: Final = "header_value"
CONF_ECONTROL_HEADER_NAME: Final = "econtrol_header_name"  # ... and for the E-Control download
CONF_ECONTROL_HEADER_VALUE: Final = "econtrol_header_value"
CONF_ECONTROL_URL: Final = "econtrol_url"
CONF_ECONTROL_MONTH_COLUMN: Final = "econtrol_month_column"
CONF_ECONTROL_VALUE_COLUMN: Final = "econtrol_value_column"
CONF_ECONTROL_BASE_COLUMN: Final = "econtrol_base_column"
CONF_ECONTROL_PUBLISHED_COLUMN: Final = "econtrol_published_column"
CONF_TIME_ZONE: Final = "time_zone"
CONF_TIMEOUT: Final = "timeout"

# entry.options: how it is calculated and how often (changed with "Configure")
CONF_DEDUCTION: Final = "deduction"
CONF_PRIOR_METHOD: Final = "prior_method"
CONF_AGGREGATION_SECONDS: Final = "aggregation_seconds"
CONF_UPDATE_INTERVAL: Final = "update_interval_minutes"
CONF_OFFICIAL_INTERVAL: Final = "official_interval_hours"
CONF_OFFICIAL_EARLY_INTERVAL: Final = "official_early_interval_hours"
CONF_REQUEST_DELAY: Final = "request_delay_seconds"
CONF_SUNRISE_OFFSET: Final = "sunrise_offset_minutes"
CONF_SUNSET_OFFSET: Final = "sunset_offset_minutes"
CONF_OVERLAP: Final = "overlap_hours"
CONF_FULL_REFRESH_TIME: Final = "full_refresh_time"
CONF_PRICE_TIME: Final = "price_time"
CONF_PRICE_RETRY: Final = "price_retry_minutes"
CONF_OFFICIAL_EARLY_DAYS: Final = "official_early_days"

DEFAULT_DATA: Final = {
    CONF_ENERGY_CHARTS_URL: DEFAULT_ENERGY_CHARTS_BASE_URL,
    CONF_GENERATION_PATH: DEFAULT_ENERGY_CHARTS_GENERATION_PATH,
    CONF_PRICE_PATH: DEFAULT_ENERGY_CHARTS_PRICE_PATH,
    CONF_COUNTRY: DEFAULT_COUNTRY,
    CONF_BIDDING_ZONE: DEFAULT_BIDDING_ZONE,
    CONF_PRODUCTION_TYPE: DEFAULT_PRODUCTION_TYPE,
    CONF_HEADER_NAME: "",
    CONF_HEADER_VALUE: "",
    CONF_ECONTROL_URL: DEFAULT_ECONTROL_XLSX_URL,
    CONF_ECONTROL_HEADER_NAME: "",
    CONF_ECONTROL_HEADER_VALUE: "",
    CONF_ECONTROL_MONTH_COLUMN: DEFAULT_ECONTROL_MONTH_COLUMN,
    CONF_ECONTROL_VALUE_COLUMN: DEFAULT_ECONTROL_COLUMN,
    CONF_ECONTROL_BASE_COLUMN: DEFAULT_ECONTROL_BASE_COLUMN,
    CONF_ECONTROL_PUBLISHED_COLUMN: DEFAULT_ECONTROL_PUBLISHED_COLUMN,
    CONF_TIME_ZONE: DEFAULT_TIME_ZONE,
    CONF_TIMEOUT: DEFAULT_REQUEST_TIMEOUT_SECONDS,
}

DEFAULT_OPTIONS: Final = {
    CONF_DEDUCTION: DEFAULT_DEDUCTION_EUR_PER_KWH,
    CONF_PRIOR_METHOD: DEFAULT_PRIOR_METHOD.value,
    CONF_AGGREGATION_SECONDS: DEFAULT_AGGREGATION_SECONDS,
    CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL_MINUTES,
    CONF_SUNRISE_OFFSET: DEFAULT_SUNRISE_OFFSET_MINUTES,
    CONF_SUNSET_OFFSET: DEFAULT_SUNSET_OFFSET_MINUTES,
    CONF_OVERLAP: DEFAULT_OVERLAP_HOURS,
    CONF_FULL_REFRESH_TIME: DEFAULT_FULL_REFRESH_TIME,
    CONF_PRICE_TIME: DEFAULT_PRICE_TIME,
    CONF_PRICE_RETRY: DEFAULT_PRICE_RETRY_MINUTES,
    CONF_OFFICIAL_INTERVAL: DEFAULT_OFFICIAL_INTERVAL_HOURS,
    CONF_OFFICIAL_EARLY_INTERVAL: DEFAULT_OFFICIAL_EARLY_INTERVAL_HOURS,
    CONF_OFFICIAL_EARLY_DAYS: DEFAULT_OFFICIAL_EARLY_DAYS,
    CONF_REQUEST_DELAY: DEFAULT_REQUEST_DELAY_SECONDS,
}


def entry_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """A config entry's data with defaults for missing keys (e.g. keys added in a later version)."""
    return {**DEFAULT_DATA, **data}


def entry_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """A config entry's options with defaults for missing keys."""
    return {**DEFAULT_OPTIONS, **options}
