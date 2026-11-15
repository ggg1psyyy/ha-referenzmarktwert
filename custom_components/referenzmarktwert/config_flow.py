"""Config flow (setup), reconfigure flow (data sources) and options flow (calculation, polling).

- Setup: the deduction, plus a collapsed "Data sources" section with every URL and parameter.
- Reconfigure (entry menu): change the data sources later.
- Configure (options): deduction, estimate method, aggregation, intervals.
Every change of the data sources is tested against the real sources before it is saved.
"""

import time
from collections.abc import Mapping
from typing import Any, Literal

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from . import ReferenzmarktwertConfigEntry
from .const import (
    CONF_AGGREGATION_SECONDS,
    CONF_BIDDING_ZONE,
    CONF_COUNTRY,
    CONF_DEDUCTION,
    CONF_ECONTROL_BASE_COLUMN,
    CONF_ECONTROL_MONTH_COLUMN,
    CONF_ECONTROL_PUBLISHED_COLUMN,
    CONF_ECONTROL_URL,
    CONF_ECONTROL_VALUE_COLUMN,
    CONF_ENERGY_CHARTS_URL,
    CONF_GENERATION_PATH,
    CONF_HEADER_NAME,
    CONF_HEADER_VALUE,
    CONF_OFFICIAL_EARLY_INTERVAL,
    CONF_OFFICIAL_INTERVAL,
    CONF_PRICE_PATH,
    CONF_PRIOR_METHOD,
    CONF_PRODUCTION_TYPE,
    CONF_REQUEST_DELAY,
    CONF_TIME_ZONE,
    CONF_TIMEOUT,
    CONF_UPDATE_INTERVAL,
    DEFAULT_DATA,
    DEFAULT_OPTIONS,
    DOMAIN,
    PriorMethod,
)
from .coordinator import econtrol_config, energy_charts_config
from .sources import (
    EnergyChartsClient,
    SourceConnectionError,
    SourceDataError,
    SourceError,
    SourceRateLimitError,
    fetch_econtrol,
)

SECTION_SOURCES = "sources"

_TEXT = TextSelector()
_URL = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _number(minimum: float, maximum: float, step: float | Literal["any"], unit: str | None = None) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=minimum, max=maximum, step=step, unit_of_measurement=unit, mode=NumberSelectorMode.BOX)
    )


def sources_schema(values: Mapping[str, Any]) -> probatio.Schema:
    """All data-source settings, pre-filled with `values`."""

    def field(key: str, selector: Any, required: bool = True) -> tuple[probatio.Marker, Any]:
        marker = probatio.Required if required else probatio.Optional
        return marker(key, default=values.get(key, DEFAULT_DATA[key])), selector

    return probatio.Schema(
        dict(
            [
                field(CONF_ENERGY_CHARTS_URL, _URL),
                field(CONF_GENERATION_PATH, _TEXT),
                field(CONF_PRICE_PATH, _TEXT),
                field(CONF_COUNTRY, _TEXT),
                field(CONF_BIDDING_ZONE, _TEXT),
                field(CONF_PRODUCTION_TYPE, _TEXT),
                field(CONF_HEADER_NAME, _TEXT, required=False),
                field(CONF_HEADER_VALUE, _SECRET, required=False),
                field(CONF_ECONTROL_URL, _URL),
                field(CONF_ECONTROL_MONTH_COLUMN, _TEXT),
                field(CONF_ECONTROL_VALUE_COLUMN, _TEXT),
                field(CONF_ECONTROL_BASE_COLUMN, _TEXT, required=False),
                field(CONF_ECONTROL_PUBLISHED_COLUMN, _TEXT, required=False),
                field(CONF_TIME_ZONE, _TEXT),
                field(CONF_TIMEOUT, _number(1, 300, 1, "s")),
            ]
        )
    )


def _deduction_field(value: float) -> dict[probatio.Marker, Any]:
    return {probatio.Required(CONF_DEDUCTION, default=value): _number(-1, 1, "any", "EUR/kWh")}


def unique_id(data: Mapping[str, Any]) -> str:
    """One entry per data series: country, bidding zone, generation type and E-Control column."""
    d = {**DEFAULT_DATA, **data}
    parts = (d[CONF_COUNTRY], d[CONF_BIDDING_ZONE], d[CONF_PRODUCTION_TYPE], d[CONF_ECONTROL_VALUE_COLUMN])
    return "_".join(str(p).strip().lower() for p in parts)


def title(data: Mapping[str, Any]) -> str:
    production = {**DEFAULT_DATA, **data}[CONF_PRODUCTION_TYPE]
    return (
        "Referenzmarktwert PV"
        if production == DEFAULT_DATA[CONF_PRODUCTION_TYPE]
        else f"Referenzmarktwert {production}"
    )


def _clean(data: Mapping[str, Any]) -> dict[str, Any]:
    """Strip text fields; optional fields left empty become ""."""
    cleaned = {key: (value.strip() if isinstance(value, str) else value) for key, value in data.items()}
    for key in (CONF_HEADER_NAME, CONF_HEADER_VALUE, CONF_ECONTROL_BASE_COLUMN, CONF_ECONTROL_PUBLISHED_COLUMN):
        cleaned.setdefault(key, "")
    return cleaned


async def validate_sources(hass: HomeAssistant, data: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Try the settings against the real sources. Returns (errors, description placeholders)."""
    if await dt_util.async_get_time_zone(data[CONF_TIME_ZONE]) is None:
        return {CONF_TIME_ZONE: "invalid_time_zone"}, {}
    session = async_get_clientsession(hass)
    client = EnergyChartsClient(session, energy_charts_config(data))
    now = int(time.time())
    try:
        # windows that certainly hold data (Energy-Charts answers 404 for windows without any)
        if not await client.generation(now - 2 * 86400, now - 86400):  # checks country and generation type
            return {"base": "invalid_data"}, {"error": "no generation data"}
        if not await client.price(now - 86400, now):  # checks the bidding zone
            return {"base": "invalid_data"}, {"error": "no price data"}
        await fetch_econtrol(session, econtrol_config(data))  # checks URL and columns
    except SourceRateLimitError as err:
        return {"base": "rate_limited"}, {"error": str(err)}
    except SourceConnectionError as err:
        invalid = err.status is not None and 400 <= err.status < 500
        return {"base": "invalid_setting" if invalid else "cannot_connect"}, {"error": str(err)}
    except SourceDataError as err:
        return {"base": "invalid_data"}, {"error": str(err)}
    except SourceError as err:  # pragma: no cover - all subclasses handled above
        return {"base": "cannot_connect"}, {"error": str(err)}
    return {}, {}


class ReferenzmarktwertConfigFlow(ConfigFlow, domain=DOMAIN):
    """Setup and reconfigure."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ReferenzmarktwertConfigEntry) -> ReferenzmarktwertOptionsFlow:
        return ReferenzmarktwertOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"error": ""}
        sources = dict(DEFAULT_DATA)
        deduction = DEFAULT_OPTIONS[CONF_DEDUCTION]
        if user_input is not None:
            sources = _clean(user_input[SECTION_SOURCES])
            deduction = user_input[CONF_DEDUCTION]
            await self.async_set_unique_id(unique_id(sources))
            self._abort_if_unique_id_configured()
            errors, found = await validate_sources(self.hass, sources)
            placeholders.update(found)
            if not errors:
                return self.async_create_entry(
                    title=title(sources),
                    data=sources,
                    options={**DEFAULT_OPTIONS, CONF_DEDUCTION: deduction},
                )
        schema = probatio.Schema(
            {
                **_deduction_field(deduction),
                probatio.Required(SECTION_SOURCES): section(sources_schema(sources), {"collapsed": not errors}),
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors, description_placeholders=placeholders
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"error": ""}
        values: Mapping[str, Any] = {**DEFAULT_DATA, **entry.data}
        if user_input is not None:
            values = _clean(user_input)
            new_id = unique_id(values)
            if new_id != entry.unique_id and any(
                other.unique_id == new_id for other in self._async_current_entries(include_ignore=False)
            ):
                return self.async_abort(reason="already_configured")
            errors, found = await validate_sources(self.hass, values)
            placeholders.update(found)
            if not errors:
                return self.async_update_reload_and_abort(entry, unique_id=new_id, title=title(values), data=values)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=sources_schema(values),
            errors=errors,
            description_placeholders=placeholders,
        )


class ReferenzmarktwertOptionsFlow(OptionsFlowWithReload):
    """Calculation and polling settings; saving reloads the entry."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        schema = probatio.Schema(
            {
                **_deduction_field(current[CONF_DEDUCTION]),
                probatio.Required(CONF_PRIOR_METHOD, default=current[CONF_PRIOR_METHOD]): SelectSelector(
                    SelectSelectorConfig(
                        options=[m.value for m in PriorMethod],
                        translation_key=CONF_PRIOR_METHOD,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                probatio.Required(CONF_AGGREGATION_SECONDS, default=current[CONF_AGGREGATION_SECONDS]): probatio.All(
                    _number(900, 3600, 900, "s"), probatio.Coerce(int)
                ),
                probatio.Required(CONF_UPDATE_INTERVAL, default=current[CONF_UPDATE_INTERVAL]): _number(
                    15, 1440, 1, "min"
                ),
                probatio.Required(CONF_OFFICIAL_INTERVAL, default=current[CONF_OFFICIAL_INTERVAL]): _number(
                    1, 168, 1, "h"
                ),
                probatio.Required(CONF_OFFICIAL_EARLY_INTERVAL, default=current[CONF_OFFICIAL_EARLY_INTERVAL]): _number(
                    1, 24, 1, "h"
                ),
                probatio.Required(CONF_REQUEST_DELAY, default=current[CONF_REQUEST_DELAY]): _number(0, 30, 0.5, "s"),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
