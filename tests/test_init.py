"""Setup, sensors, download schedule, stored data, failures and unloading in a real (test) Home Assistant."""

from datetime import UTC, datetime, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.referenzmarktwert import calc
from custom_components.referenzmarktwert.const import DEFAULT_ECONTROL_XLSX_URL, DOMAIN
from custom_components.referenzmarktwert.coordinator import storage_key
from custom_components.referenzmarktwert.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    FIXTURES,
    GENERATION_LAG,
    GENERATION_URL,
    LAST_MODIFIED,
    PRICE_URL,
    VIENNA,
    econtrol_xlsx,
    fake_sources,
    fixture_json,
    requests_to,
)

pytestmark = pytest.mark.usefixtures("vienna")

# Late on 31 October 2026 (Vienna 22:30): the published data covers the whole month's daylight.
NOW = "2026-10-31T21:30:00+00:00"
OCTOBER = calc.month_bounds((2026, 10), VIENNA)
OCTOBER_2025 = calc.month_bounds((2025, 10), VIENNA)


def ts(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp())


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def wait_until(hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: str) -> None:
    """Let time pass until `when`, firing every scheduled update on the way."""
    target = datetime.fromisoformat(when)
    while (now := datetime.now(target.tzinfo)) < target:
        freezer.tick(min(timedelta(minutes=5), target - now))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


def energy_charts_calls(mock: AiohttpClientMocker) -> int:
    return len(requests_to(mock, GENERATION_URL)) + len(requests_to(mock, PRICE_URL))


@pytest.mark.freeze_time(NOW)
async def test_setup_creates_sensors(hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts) -> None:
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


@pytest.mark.freeze_time("2026-10-15T09:00:00+00:00")  # 11:00 in Vienna, daylight
async def test_daytime_downloads_only_what_is_new(
    hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts, freezer: FrozenDateTimeFactory
) -> None:
    now = ts("2026-10-15T09:00:00+00:00")
    await setup(hass, config_entry)
    # first run: the month so far, prices until what is published, and October 2025 once
    assert requests_to(energy_charts, GENERATION_URL) == [
        {"start": OCTOBER[0], "end": now},
        {"start": OCTOBER_2025[0], "end": OCTOBER_2025[1]},
    ]
    assert requests_to(energy_charts, PRICE_URL) == [{"start": OCTOBER[0], "end": now + 2 * 86400}]

    # an hour later: only generation, from the last stored point minus the 3 h overlap
    energy_charts.mock_calls.clear()
    await wait_until(hass, freezer, "2026-10-15T10:01:00+00:00")
    last_point = calc.floor_to(now - GENERATION_LAG, 900)
    assert requests_to(energy_charts, GENERATION_URL) == [{"start": last_point - 3 * 3600, "end": now + 3600}]
    assert requests_to(energy_charts, PRICE_URL) == []  # today's prices are there, tomorrow's not out yet

    # 13:30 Vienna: tomorrow's prices, starting after the last stored one
    energy_charts.mock_calls.clear()
    await wait_until(hass, freezer, "2026-10-15T11:31:00+00:00")
    prices = requests_to(energy_charts, PRICE_URL)
    assert [p["start"] for p in prices] == [ts("2026-10-16T00:00:00+02:00")]


@pytest.mark.freeze_time("2026-10-15T20:30:00+00:00")  # 22:30 in Vienna, after the daylight window
async def test_night_downloads_nothing_until_the_full_download(
    hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts, freezer: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    energy_charts.mock_calls.clear()

    await wait_until(hass, freezer, "2026-10-15T23:55:00+00:00")  # 01:55 Vienna
    assert energy_charts_calls(energy_charts) == 0

    # 02:00 Vienna: one full download of the running month, nothing else
    await wait_until(hass, freezer, "2026-10-16T00:05:00+00:00")
    assert requests_to(energy_charts, GENERATION_URL) == [{"start": OCTOBER[0], "end": ts("2026-10-16T00:00:00+00:00")}]
    assert requests_to(energy_charts, PRICE_URL) == []

    # the morning's first download starts at the last stored point minus the overlap: the hours
    # around dusk that were published after the window closed are fetched, too
    energy_charts.mock_calls.clear()
    await wait_until(hass, freezer, "2026-10-16T07:00:00+00:00")  # 09:00 Vienna
    first = requests_to(energy_charts, GENERATION_URL)[0]
    last_night = calc.floor_to(ts("2026-10-16T00:00:00+00:00") - GENERATION_LAG, 900)
    assert first["start"] == last_night - 3 * 3600


@pytest.mark.freeze_time("2026-10-15T09:00:00+00:00")
async def test_restart_costs_no_downloads(
    hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts, freezer: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    value = hass.states.get("sensor.referenzmarktwert_pv_estimate_running_month").state
    energy_charts.mock_calls.clear()

    freezer.tick(timedelta(minutes=10))
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert energy_charts.mock_calls == []  # neither Energy-Charts nor E-Control
    assert hass.states.get("sensor.referenzmarktwert_pv_estimate_running_month").state == value


@pytest.mark.freeze_time(NOW)
async def test_econtrol_only_downloaded_if_changed(
    hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts, freezer: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    energy_charts.clear_requests()
    energy_charts.get(DEFAULT_ECONTROL_XLSX_URL, status=304)
    energy_charts.get(GENERATION_URL, status=404, text="no content available")
    energy_charts.get(PRICE_URL, status=404, text="no content available")
    await wait_until(hass, freezer, "2026-11-01T21:31:00+00:00")  # a day later
    checks = [headers for _, url, _, headers in energy_charts.mock_calls if str(url) == DEFAULT_ECONTROL_XLSX_URL]
    assert checks
    assert all(h["If-Modified-Since"] == LAST_MODIFIED for h in checks)
    official = hass.states.get("sensor.referenzmarktwert_pv_official_value")
    assert float(official.state) == pytest.approx(0.1771)  # the stored table is kept


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
    assert config_entry.state is ConfigEntryState.SETUP_RETRY  # nothing downloaded yet
    assert "rate limited (HTTP 429)" in caplog.text
    assert "Unexpected error" not in caplog.text


@pytest.mark.freeze_time("2026-10-15T09:00:00+00:00")
async def test_energy_charts_outage_keeps_stored_data(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    energy_charts,
    freezer: FrozenDateTimeFactory,
    caplog,
) -> None:
    await setup(hass, config_entry)
    energy_charts.clear_requests()
    energy_charts.get(GENERATION_URL, status=503)
    energy_charts.get(PRICE_URL, status=503)
    energy_charts.get(DEFAULT_ECONTROL_XLSX_URL, status=304)
    await wait_until(hass, freezer, "2026-10-15T10:01:00+00:00")
    estimate = hass.states.get("sensor.referenzmarktwert_pv_estimate_running_month")
    assert estimate.state not in ("unavailable", "unknown")
    assert caplog.text.count("Energy-Charts not available") == 1


@pytest.mark.freeze_time(NOW)
async def test_econtrol_failure_is_retried_on_next_update(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    energy_charts,
    freezer: FrozenDateTimeFactory,
) -> None:
    energy_charts.clear_requests()
    energy_charts.get(PRICE_URL, json=fixture_json("price_at_2026-10.json"))
    energy_charts.get(GENERATION_URL, json=fixture_json("public_power_at_2026-10.json"))
    energy_charts.get(DEFAULT_ECONTROL_XLSX_URL, status=503)
    await setup(hass, config_entry)
    assert hass.states.get("sensor.referenzmarktwert_pv_official_value").state == "unknown"

    energy_charts.clear_requests()
    energy_charts.get(PRICE_URL, json=fixture_json("price_at_2026-10.json"))
    energy_charts.get(GENERATION_URL, json=fixture_json("public_power_at_2026-10.json"))
    energy_charts.get(DEFAULT_ECONTROL_XLSX_URL, content=(FIXTURES / "Referenzmarktwert_Entwicklung.xlsx").read_bytes())
    await wait_until(hass, freezer, "2026-10-31T22:31:00+00:00")  # an hour later, not a day
    assert float(hass.states.get("sensor.referenzmarktwert_pv_official_value").state) == pytest.approx(0.1771)


@pytest.mark.freeze_time(NOW)
async def test_econtrol_outage_keeps_estimate(
    hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts, caplog
) -> None:
    """Without the official file the estimate still works (month so far); official is unknown."""
    energy_charts.clear_requests()
    energy_charts.get(PRICE_URL, json=fixture_json("price_at_2026-10.json"))
    energy_charts.get(GENERATION_URL, json=fixture_json("public_power_at_2026-10.json"))
    energy_charts.get(DEFAULT_ECONTROL_XLSX_URL, status=503)
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert float(hass.states.get("sensor.referenzmarktwert_pv_estimate_running_month").state) == pytest.approx(
        0.1771, abs=0.0001
    )
    assert hass.states.get("sensor.referenzmarktwert_pv_official_value").state == "unknown"
    assert "E-Control file not available" in caplog.text


@pytest.mark.freeze_time(NOW)
async def test_unload_and_remove(
    hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts, hass_storage
) -> None:
    await setup(hass, config_entry)
    assert storage_key(config_entry.entry_id) in hass_storage
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert storage_key(config_entry.entry_id) in hass_storage  # kept for the next start

    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert storage_key(config_entry.entry_id) not in hass_storage  # deleted with the entry


@pytest.mark.freeze_time(NOW)
async def test_diagnostics_redacts_headers(hass: HomeAssistant, energy_charts) -> None:
    from custom_components.referenzmarktwert.const import (
        CONF_ECONTROL_HEADER_NAME,
        CONF_ECONTROL_HEADER_VALUE,
        CONF_HEADER_NAME,
        CONF_HEADER_VALUE,
        DEFAULT_DATA,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Referenzmarktwert PV",
        data={
            **DEFAULT_DATA,
            CONF_HEADER_NAME: "X-Api-Key",
            CONF_HEADER_VALUE: "secret",
            CONF_ECONTROL_HEADER_NAME: "X-Other",
            CONF_ECONTROL_HEADER_VALUE: "hidden",
        },
        options={"request_delay_seconds": 0},
    )
    await setup(hass, entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["data"][CONF_HEADER_VALUE] == "**REDACTED**"
    assert diagnostics["data"][CONF_ECONTROL_HEADER_VALUE] == "**REDACTED**"
    assert diagnostics["estimate"]["month"] == (2026, 10)
    assert diagnostics["stored"]["generation_since"] == OCTOBER[0]
    # the headers are really sent, each only to its own source
    calls = energy_charts.mock_calls
    assert any(h and h.get("X-Api-Key") == "secret" for _, url, _, h in calls if str(url).startswith(PRICE_URL))
    assert any(h and h.get("X-Other") == "hidden" for _, url, _, h in calls if str(url) == DEFAULT_ECONTROL_XLSX_URL)
    assert not any(h and "X-Other" in h for _, url, _, h in calls if str(url).startswith(PRICE_URL))


@pytest.mark.freeze_time(NOW)
async def test_econtrol_failure_is_retried_after_a_reload(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker, freezer
) -> None:
    """The failed attempt is stored; after a reload (e.g. saving the options) it must still be retried
    every interval, not only a day later."""
    fake_sources(aioclient_mock, b"not available")  # E-Control answers with garbage
    await setup(hass, config_entry)
    assert hass.states.get("sensor.referenzmarktwert_pv_official_value").state == "unknown"
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    aioclient_mock.clear_requests()
    fake_sources(aioclient_mock, econtrol_xlsx())
    await wait_until(hass, freezer, "2026-10-31T22:31:00+00:00")  # an hour later
    assert float(hass.states.get("sensor.referenzmarktwert_pv_official_value").state) == pytest.approx(0.1771)


@pytest.mark.freeze_time("2026-09-30T10:00:00+00:00")  # 12:00 Vienna; E-Control has published August
async def test_month_change(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker, freezer
) -> None:
    fake_sources(aioclient_mock, econtrol_xlsx(until=(2026, 8)))
    september = calc.month_bounds((2026, 9), VIENNA)
    await setup(hass, config_entry)
    assert requests_to(aioclient_mock, GENERATION_URL)[0]["start"] == september[0]

    # 13:30: tomorrow's prices, although tomorrow is in the next month
    await wait_until(hass, freezer, "2026-09-30T11:31:00+00:00")
    assert requests_to(aioclient_mock, PRICE_URL)[-1]["end"] > september[1]
    assert (
        calc.covered_until(
            hass.config_entries.async_get_entry(config_entry.entry_id).runtime_data.state.price.series(), 900
        )
        == OCTOBER[0] + 86400
    )

    # midnight: the estimate switches to October; only last year's October is downloaded
    await wait_until(hass, freezer, "2026-09-30T21:55:00+00:00")
    aioclient_mock.mock_calls.clear()
    await wait_until(hass, freezer, "2026-09-30T22:01:00+00:00")
    assert requests_to(aioclient_mock, GENERATION_URL) == [{"start": OCTOBER_2025[0], "end": OCTOBER_2025[1]}]
    assert requests_to(aioclient_mock, PRICE_URL) == []
    estimate = hass.states.get("sensor.referenzmarktwert_pv_estimate_running_month")
    assert estimate.attributes["month"] == "2026-10"
    assert estimate.attributes["previous_month_source"] == "recomputed"  # from the stored September
    assert estimate.attributes["previous_month"] == pytest.approx(0.1053, abs=0.0001)
    assert estimate.attributes["prices_until"] == datetime.fromtimestamp(OCTOBER[0] + 86400, UTC).isoformat()

    # 02:00: the nightly full download includes September (not official yet)
    aioclient_mock.mock_calls.clear()
    await wait_until(hass, freezer, "2026-10-01T00:05:00+00:00")
    assert requests_to(aioclient_mock, GENERATION_URL)[0]["start"] == september[0]


@pytest.mark.freeze_time(NOW)
async def test_deprecation_notice(hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock) -> None:
    """Energy-Charts announces the end of an endpoint with "deprecated": true: a repair notice appears."""
    from homeassistant.helpers import issue_registry as ir

    generation = {**fixture_json("public_power_at_2026-10.json"), "deprecated": True}
    price = {**fixture_json("price_at_2026-10.json"), "deprecated": True}
    aioclient_mock.get(GENERATION_URL, json=generation)
    aioclient_mock.get(PRICE_URL, json=price)
    aioclient_mock.get(DEFAULT_ECONTROL_XLSX_URL, content=econtrol_xlsx())
    await setup(hass, config_entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"energy_charts_deprecated_{config_entry.entry_id}")
    assert issue is not None
    assert issue.translation_key == "energy_charts_deprecated"
    assert config_entry.state is ConfigEntryState.LOADED  # it still works


@pytest.mark.freeze_time(NOW)
async def test_no_deprecation_notice(hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts) -> None:
    from homeassistant.helpers import issue_registry as ir

    await setup(hass, config_entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"energy_charts_deprecated_{config_entry.entry_id}") is None
