"""Constants and defaults for the Referenzmarktwert integration.

Everything here is a *default*: the config/options flow lets the user override each value.
This module must stay free of Home Assistant imports so the pure modules (calc, sources) can use it.
"""

from typing import Final

DOMAIN: Final = "referenzmarktwert"

# --- Calculation -------------------------------------------------------------------------------
# E-Control weights *hourly* values (§ 13 EAG), even though the source data is quarter-hourly.
DEFAULT_AGGREGATION_SECONDS: Final = 3600

# Feed-in price = reference market value - deduction (Hofer "Sonne": 1.5 ct/kWh, CPI-indexed).
# All prices are in the main currency unit (EUR), as Home Assistant's Energy Dashboard requires.
DEFAULT_DEDUCTION_EUR_PER_KWH: Final = 0.015

# --- Energy-Charts (Fraunhofer ISE), data licensed CC BY 4.0 ---------------------------------
DEFAULT_ENERGY_CHARTS_BASE_URL: Final = "https://api.energy-charts.info"
DEFAULT_COUNTRY: Final = "at"
DEFAULT_BIDDING_ZONE: Final = "AT"
DEFAULT_PRODUCTION_TYPE: Final = "Solar"

# --- E-Control official values ------------------------------------------------------------------
DEFAULT_ECONTROL_XLSX_URL: Final = (
    "https://www.e-control.at/documents/1785851/10823410/Referenzmarktwert_Entwicklung.xlsx"
)
DEFAULT_ECONTROL_COLUMN: Final = "D"  # B hydro, C wind, D photovoltaics

# --- Time --------------------------------------------------------------------------------------
DEFAULT_TIME_ZONE: Final = "Europe/Vienna"  # months are assigned in this zone

# --- Units ---------------------------------------------------------------------------------------
# Exchange prices arrive in EUR/MWh; sensors and statistics use EUR/kWh.
KWH_PER_MWH: Final = 1000.0
