"""Constants and defaults for the Referenzmarktwert integration.

Everything here is a *default*: the config/options flow lets the user override each value.
This module must stay free of Home Assistant imports so the pure modules (calc, sources) can use it.
"""

from enum import StrEnum
from typing import Final

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

# --- Time --------------------------------------------------------------------------------------
DEFAULT_TIME_ZONE: Final = "Europe/Vienna"  # months are assigned in this zone

# --- Units ---------------------------------------------------------------------------------------
# Exchange prices arrive in EUR/MWh; sensors and statistics use EUR/kWh.
KWH_PER_MWH: Final = 1000.0

# --- Polling -------------------------------------------------------------------------------------
DEFAULT_UPDATE_INTERVAL_MINUTES: Final = 60  # Energy-Charts data; generation lags ~2 h anyway
DEFAULT_OFFICIAL_INTERVAL_HOURS: Final = 24  # E-Control file, normally
DEFAULT_OFFICIAL_EARLY_INTERVAL_HOURS: Final = 3  # ... while last month's value is awaited
OFFICIAL_EARLY_DAYS: Final = 10  # "awaited": until this day of the month (observed: 2nd-7th)
DEFAULT_REQUEST_DELAY_SECONDS: Final = 1.0  # pause between Energy-Charts requests (rate limit)

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
    CONF_OFFICIAL_INTERVAL: DEFAULT_OFFICIAL_INTERVAL_HOURS,
    CONF_OFFICIAL_EARLY_INTERVAL: DEFAULT_OFFICIAL_EARLY_INTERVAL_HOURS,
    CONF_REQUEST_DELAY: DEFAULT_REQUEST_DELAY_SECONDS,
}
