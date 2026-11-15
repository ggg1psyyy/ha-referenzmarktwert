"""Setup, sensors, caching, failures and unloading in a real (test) Home Assistant."""

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.referenzmarktwert.const import DEFAULT_ECONTROL_XLSX_URL
from custom_components.referenzmarktwert.diagnostics import async_get_config_entry_diagnostics

from .conftest import FIXTURES, GENERATION_URL, PRICE_URL, fixture_json

# Late on 31 October 2026 (Vienna): the recorded data covers the whole month.
NOW = "2026-10-31T21:30:00+00:00"


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def energy_charts_calls(mock: AiohttpClientMocker) -> int:
    return sum(1 for _, url, _, _ in mock.mock_calls if str(url).startswith((PRICE_URL, GENERATION_URL)))


@pytest.mark.freeze_time(NOW)
async def test_setup_creates_sensors(hass: HomeAssistant, config_entry: MockConfigEntry, mock_sources) -> None:
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    estimate = hass.states.get("sensor.referenzmarktwert_pv_estimate_running_month")
    assert estimate is not None
    assert float(estimate.state) == pytest.approx(0.1771, abs=0.0001)  # whole month known
    assert estimate.attributes["unit_of_measurement"] == "EUR/kWh"
    assert estimate.attributes["state_class"] == "measurement"
    assert estimate.attributes["month"] == "2026-10"
    assert estimate.attributes["previous_month_source"] == "official"
    assert estimate.attributes["known_share"] == 1.0  # reference month (October 2025) was used

    payout = hass.states.get("sensor.referenzmarktwert_pv_payout_estimate_running_month")
    assert float(payout.state) == pytest.approx(0.1771 - 0.015, abs=0.0001)

    official = hass.states.get("sensor.referenzmarktwert_pv_official_value")
    assert float(official.state) == pytest.approx(0.1771)
    assert official.attributes["month"] == "2026-10"
    assert official.attributes["published"] == "2026-11-04"
    assert official.attributes["history"]["2022-01"] == pytest.approx(0.206)
    assert float(hass.states.get("sensor.referenzmarktwert_pv_payout_official_value").state) == pytest.approx(0.1621)

    registry = er.async_get(hass)
    assert registry.async_get("sensor.referenzmarktwert_pv_estimate_running_month").unique_id == (
        f"{config_entry.entry_id}_estimate"
    )


@pytest.mark.freeze_time(NOW)
async def test_updates_use_cache(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_sources, freezer: FrozenDateTimeFactory
) -> None:
    """First refresh: month + reference month; later only the running month (2 requests)."""
    await setup(hass, config_entry)
    first = energy_charts_calls(mock_sources)
    assert first == 3  # generation + price of October, generation of October 2025
    freezer.tick(timedelta(minutes=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert energy_charts_calls(mock_sources) == first + 2


@pytest.mark.freeze_time(NOW)
async def test_rate_limit_retries_later(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker, caplog
) -> None:
    aioclient_mock.get(PRICE_URL, json=fixture_json("price_at_2026-10.json"))
    aioclient_mock.get(GENERATION_URL, status=429, headers={"Retry-After": "30"})
    aioclient_mock.get(
        DEFAULT_ECONTROL_XLSX_URL, content=(FIXTURES / "Referenzmarktwert_Entwicklung.xlsx").read_bytes()
    )
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert "rate limited (HTTP 429)" in caplog.text
    assert "Unexpected error" not in caplog.text


@pytest.mark.freeze_time(NOW)
async def test_econtrol_failure_is_retried_on_next_update(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    aioclient_mock.get(PRICE_URL, json=fixture_json("price_at_2026-10.json"))
    aioclient_mock.get(GENERATION_URL, json=fixture_json("public_power_at_2026-10.json"))
    aioclient_mock.get(DEFAULT_ECONTROL_XLSX_URL, status=503)
    await setup(hass, config_entry)
    assert hass.states.get("sensor.referenzmarktwert_pv_official_value").state == "unknown"

    aioclient_mock.clear_requests()
    aioclient_mock.get(PRICE_URL, json=fixture_json("price_at_2026-10.json"))
    aioclient_mock.get(GENERATION_URL, json=fixture_json("public_power_at_2026-10.json"))
    aioclient_mock.get(
        DEFAULT_ECONTROL_XLSX_URL, content=(FIXTURES / "Referenzmarktwert_Entwicklung.xlsx").read_bytes()
    )
    freezer.tick(timedelta(minutes=61))  # next update, not a day later
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.referenzmarktwert_pv_official_value").state) == pytest.approx(0.1771)


@pytest.mark.freeze_time(NOW)
async def test_econtrol_outage_keeps_estimate(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker, caplog
) -> None:
    """Without the official file the estimate still works (month so far); official is unknown."""
    aioclient_mock.get(PRICE_URL, json=fixture_json("price_at_2026-10.json"))
    aioclient_mock.get(GENERATION_URL, json=fixture_json("public_power_at_2026-10.json"))
    aioclient_mock.get(DEFAULT_ECONTROL_XLSX_URL, status=503)
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert float(hass.states.get("sensor.referenzmarktwert_pv_estimate_running_month").state) == pytest.approx(
        0.1771, abs=0.0001
    )
    assert hass.states.get("sensor.referenzmarktwert_pv_official_value").state == "unknown"
    assert "E-Control file not available" in caplog.text


@pytest.mark.freeze_time(NOW)
async def test_unload(hass: HomeAssistant, config_entry: MockConfigEntry, mock_sources) -> None:
    await setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.freeze_time(NOW)
async def test_diagnostics_redacts_header(hass: HomeAssistant, mock_sources) -> None:
    from custom_components.referenzmarktwert.const import CONF_HEADER_NAME, CONF_HEADER_VALUE, DEFAULT_DATA, DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Referenzmarktwert PV",
        data={**DEFAULT_DATA, CONF_HEADER_NAME: "X-Api-Key", CONF_HEADER_VALUE: "secret"},
        options={"request_delay_seconds": 0},
    )
    await setup(hass, entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["data"][CONF_HEADER_VALUE] == "**REDACTED**"
    assert diagnostics["estimate"]["month"] == (2026, 10)
    # the header is really sent
    assert any(headers and headers.get("X-Api-Key") == "secret" for _, _, _, headers in mock_sources.mock_calls)
