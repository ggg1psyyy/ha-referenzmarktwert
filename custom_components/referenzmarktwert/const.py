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
    BASE_X_CAPTURE_RATE 2.17, PREVIOUS_MONTH 2.67, SEASONAL_DIFFERENCE 6.39. Hourly, whole estimate
    on day 1/10/20: 2.17/1.69/1.10 vs 2.70/2.18/1.39 vs 6.27/4.51/2.40.
    """

    # price level (previous month's base, moving to the running month's) x the same calendar
    # month's PV/base ratio (capture rate) last year
    BASE_X_CAPTURE_RATE = "base_x_capture_rate"
    # previous month's PV value as is
    PREVIOUS_MONTH = "previous_month"
    # previous month's PV value + last year's change from that month to this one
    SEASONAL_DIFFERENCE = "seasonal_difference"


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
