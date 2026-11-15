"""Config, reconfigure and options flow."""

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.referenzmarktwert.config_flow import SECTION_SOURCES
from custom_components.referenzmarktwert.const import (
    CONF_BIDDING_ZONE,
    CONF_DEDUCTION,
    CONF_PRIOR_METHOD,
    CONF_PRODUCTION_TYPE,
    CONF_REQUEST_DELAY,
    CONF_TIME_ZONE,
    DEFAULT_DATA,
    DEFAULT_ECONTROL_XLSX_URL,
    DEFAULT_OPTIONS,
    DOMAIN,
)

from .conftest import GENERATION_URL, PRICE_URL

SETUP = "custom_components.referenzmarktwert.async_setup_entry"


def sources(**changes) -> dict:
    return {**DEFAULT_DATA, **changes}


async def start(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return result


async def test_user_flow_creates_entry(hass: HomeAssistant, mock_sources) -> None:
    result = await start(hass)
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEDUCTION: 0.02, SECTION_SOURCES: sources()}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Referenzmarktwert PV"
    assert result["data"][CONF_BIDDING_ZONE] == "AT"
    assert result["options"] == {**DEFAULT_OPTIONS, CONF_DEDUCTION: 0.02}
    assert result["result"].unique_id == "at_at_solar_d"


async def test_user_flow_other_technology_title(hass: HomeAssistant, mock_sources, aioclient_mock) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        GENERATION_URL, json={"unix_seconds": [1], "production_types": [{"name": "Wind onshore", "data": [1.0]}]}
    )
    aioclient_mock.get(PRICE_URL, json={"unix_seconds": [1], "price": [1.0], "unit": "EUR / MWh"})
    from .conftest import FIXTURES

    aioclient_mock.get(
        DEFAULT_ECONTROL_XLSX_URL, content=(FIXTURES / "Referenzmarktwert_Entwicklung.xlsx").read_bytes()
    )
    result = await start(hass)
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_DEDUCTION: 0, SECTION_SOURCES: sources(**{CONF_PRODUCTION_TYPE: "Wind onshore"})},
        )
    assert result["title"] == "Referenzmarktwert Wind onshore"


async def test_user_flow_already_configured(hass: HomeAssistant, config_entry: MockConfigEntry, mock_sources) -> None:
    config_entry.add_to_hass(hass)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEDUCTION: 0.015, SECTION_SOURCES: sources()}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("url", "mock", "error"),
    [
        (GENERATION_URL, {"exc": TimeoutError()}, "cannot_connect"),
        (GENERATION_URL, {"status": 500}, "cannot_connect"),
        (PRICE_URL, {"status": 400, "text": "'XX' is not the code for an available bidding zone"}, "invalid_setting"),
        (GENERATION_URL, {"status": 429}, "rate_limited"),
        (GENERATION_URL, {"json": {"unix_seconds": [], "production_types": []}}, "invalid_data"),
        (DEFAULT_ECONTROL_XLSX_URL, {"content": b"not an xlsx"}, "invalid_data"),
    ],
)
async def test_user_flow_errors_recover(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, url: str, mock: dict, error: str
) -> None:
    from .conftest import FIXTURES, fixture_json

    good = {
        PRICE_URL: {"json": fixture_json("price_at_2026-10.json")},
        GENERATION_URL: {"json": fixture_json("public_power_at_2026-10.json")},
        DEFAULT_ECONTROL_XLSX_URL: {"content": (FIXTURES / "Referenzmarktwert_Entwicklung.xlsx").read_bytes()},
    }
    for target, response in good.items():
        aioclient_mock.get(target, **(mock if target == url else response))
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEDUCTION: 0.015, SECTION_SOURCES: sources()}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert result["description_placeholders"]["error"]

    # the problem goes away: the same flow finishes
    aioclient_mock.clear_requests()
    for target, response in good.items():
        aioclient_mock.get(target, **response)
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEDUCTION: 0.015, SECTION_SOURCES: sources()}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_invalid_time_zone(hass: HomeAssistant, mock_sources) -> None:
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEDUCTION: 0.015, SECTION_SOURCES: sources(**{CONF_TIME_ZONE: "Mars/Olympus"})}
    )
    assert result["errors"] == {CONF_TIME_ZONE: "invalid_time_zone"}
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEDUCTION: 0.015, SECTION_SOURCES: sources()}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_reconfigure(hass: HomeAssistant, config_entry: MockConfigEntry, mock_sources) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], sources(**{CONF_BIDDING_ZONE: "DE-LU"})
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_BIDDING_ZONE] == "DE-LU"
    assert config_entry.unique_id == "at_de-lu_solar_d"


async def test_reconfigure_to_existing_series_aborts(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_sources
) -> None:
    config_entry.add_to_hass(hass)
    other = MockConfigEntry(domain=DOMAIN, unique_id="at_at_wind_d", data=sources(**{CONF_PRODUCTION_TYPE: "Wind"}))
    other.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], sources(**{CONF_PRODUCTION_TYPE: "Wind"})
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_error_recovers(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker, mock_sources
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], sources(**{CONF_TIME_ZONE: "Nowhere"}))
    assert result["errors"] == {CONF_TIME_ZONE: "invalid_time_zone"}
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], sources())
    assert result["reason"] == "reconfigure_successful"


async def test_options_flow(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    new = {**DEFAULT_OPTIONS, CONF_DEDUCTION: 0.016, CONF_PRIOR_METHOD: "previous_month", CONF_REQUEST_DELAY: 2}
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.options.async_configure(result["flow_id"], new)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_DEDUCTION] == 0.016
    assert config_entry.options[CONF_PRIOR_METHOD] == "previous_month"


async def test_forms_serialize_for_the_frontend(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """The frontend receives the forms through the same serializer as HA's flow HTTP views."""
    import probatio
    from homeassistant.helpers import config_validation as cv

    def fields(result: dict) -> list[dict]:
        return probatio.to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)

    user = fields(await start(hass))
    assert [f["name"] for f in user] == [CONF_DEDUCTION, SECTION_SOURCES]
    assert user[1]["type"] == "expandable"
    assert len(user[1]["schema"]) == len(DEFAULT_DATA)

    config_entry.add_to_hass(hass)
    reconfigure = fields(await config_entry.start_reconfigure_flow(hass))
    assert {f["name"] for f in reconfigure} == set(DEFAULT_DATA)

    options = fields(await hass.config_entries.options.async_init(config_entry.entry_id))
    assert {f["name"] for f in options} == set(DEFAULT_OPTIONS)
