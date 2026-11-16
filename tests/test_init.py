"""Setup, sensors, download schedule, stored data, failures and unloading in a real (test) Home Assistant."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.referenzmarktwert import calc
from custom_components.referenzmarktwert.cache import STORAGE_VERSION
from custom_components.referenzmarktwert.const import (
    CONF_ERROR_AT_END,
    CONF_ERROR_AT_START,
    CONF_RANGE_WIDTH,
    CONF_REQUEST_DELAY,
    DEFAULT_ECONTROL_XLSX_URL,
    DEFAULT_ENERGY_CHARTS_BASE_URL,
    DOMAIN,
)
from custom_components.referenzmarktwert.coordinator import deprecation_issue_id, schedule_settings, storage_key
from custom_components.referenzmarktwert.diagnostics import async_get_config_entry_diagnostics
from custom_components.referenzmarktwert.schedule import jitter

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
    assert estimate.attributes["calc_known_share"] == 1.0  # reference month (October 2025) was used

    assert estimate.attributes["range_error"] == pytest.approx(0.0025)  # whole month known: only the rest
    assert estimate.attributes["range_low"] == pytest.approx(float(estimate.state) - 0.0025, abs=1e-6)
    assert estimate.attributes["range_high"] == pytest.approx(float(estimate.state) + 0.0025, abs=1e-6)

    payout = hass.states.get("sensor.referenzmarktwert_pv_payout_estimate_running_month")
    assert float(payout.state) == pytest.approx(0.1771 - 0.015, abs=0.0001)
    assert payout.attributes["range_high"] - payout.attributes["range_low"] == pytest.approx(2 * 0.0025)  # same width
    assert payout.attributes["range_low"] == pytest.approx(estimate.attributes["range_low"] - 0.015)

    error = hass.states.get("sensor.referenzmarktwert_pv_expected_error_of_the_estimate")
    assert float(error.state) == pytest.approx(0.0025)
    assert error.attributes["unit_of_measurement"] == "EUR/kWh"

    official = hass.states.get("sensor.referenzmarktwert_pv_official_value_latest")
    assert float(official.state) == pytest.approx(0.1771)
    assert official.attributes["month"] == "2026-10"
    assert official.attributes["month_published"] == "2026-11-04"
    assert official.attributes["history"]["2022-01"] == pytest.approx(0.206)
    # names start with their group: Home Assistant sorts attributes by name
    assert sorted(estimate.attributes) == [
        "calc_known_share", "calc_method", "calc_month_so_far", "calc_prior", "calc_running_base_price",
        "data_until_generation", "data_until_prices", "friendly_name", "month",
        "previous_month_source", "previous_month_value", "range_error", "range_high", "range_low",
        "state_class", "unit_of_measurement",
    ]  # fmt: skip
    assert sorted(official.attributes) == [
        "file_checked", "friendly_name", "history", "month", "month_base_price", "month_published",
        "state_class", "unit_of_measurement",
    ]  # fmt: skip
    payout_official = hass.states.get("sensor.referenzmarktwert_pv_payout_official_value_latest")
    assert float(payout_official.state) == pytest.approx(0.1621)

    registry = er.async_get(hass)
    assert registry.async_get("sensor.referenzmarktwert_pv_estimate_running_month").unique_id == (
        f"{config_entry.entry_id}_estimate"
    )


@pytest.mark.freeze_time(NOW)
async def test_expected_error_options(hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Referenzmarktwert PV",
        data=dict(config_entry.data),
        options={**config_entry.options, CONF_ERROR_AT_START: 0.04, CONF_ERROR_AT_END: 0.01},
    )
    await setup(hass, entry)
    error = hass.states.get("sensor.referenzmarktwert_pv_expected_error_of_the_estimate")
    assert float(error.state) == pytest.approx(0.01)  # the whole month is known: only the end value


BOUNDS = {
    "sensor.referenzmarktwert_pv_estimate_running_month_low": ("estimate", -1),
    "sensor.referenzmarktwert_pv_estimate_running_month_high": ("estimate", 1),
    "sensor.referenzmarktwert_pv_payout_estimate_running_month_low": ("payout_estimate", -1),
    "sensor.referenzmarktwert_pv_payout_estimate_running_month_high": ("payout_estimate", 1),
}


@pytest.mark.freeze_time(NOW)
async def test_bound_sensors_are_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts
) -> None:
    await setup(hass, config_entry)
    registry = er.async_get(hass)
    for entity_id in BOUNDS:
        assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None


@pytest.mark.freeze_time(NOW)
async def test_range_width_sets_attributes_and_bound_sensors(
    hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts
) -> None:
    """One width for all: the main sensors' range attributes and the (enabled) bound sensors."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Referenzmarktwert PV",
        data=dict(config_entry.data),
        options={**config_entry.options, CONF_RANGE_WIDTH: 2.0},
    )
    await setup(hass, entry)
    registry = er.async_get(hass)
    for entity_id in BOUNDS:
        registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    estimate = hass.states.get("sensor.referenzmarktwert_pv_estimate_running_month")
    error = float(hass.states.get("sensor.referenzmarktwert_pv_expected_error_of_the_estimate").state)
    assert error == pytest.approx(0.0025)  # the expected error itself does not change
    assert estimate.attributes["range_error"] == pytest.approx(2 * error)
    assert estimate.attributes["range_low"] == pytest.approx(float(estimate.state) - 2 * error, abs=1e-6)
    payout = hass.states.get("sensor.referenzmarktwert_pv_payout_estimate_running_month")
    assert payout.attributes["range_high"] == pytest.approx(float(payout.state) + 2 * error, abs=1e-6)
    for entity_id, (main, side) in BOUNDS.items():
        state = hass.states.get(entity_id)
        main_state = hass.states.get(f"sensor.referenzmarktwert_pv_{main}_running_month")
        assert float(state.state) == pytest.approx(float(main_state.state) + side * 2 * error, abs=1e-6)
        assert float(state.state) == pytest.approx(main_state.attributes["range_low" if side < 0 else "range_high"])
        assert state.attributes["unit_of_measurement"] == "EUR/kWh"
        assert state.attributes["month"] == "2026-10"
        assert state.attributes["range_error"] == pytest.approx(2 * error)


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


@pytest.mark.freeze_time("2026-11-01T09:00:00+00:00")  # 10:00 Vienna on the 1st, October is official
async def test_first_download_includes_yesterday(
    hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts, freezer: FrozenDateTimeFactory
) -> None:
    """Yesterday's generation sets the download window, so it is fetched even when its month is done."""
    await setup(hass, config_entry)
    yesterday = ts("2026-10-31T00:00:00+01:00")
    assert requests_to(energy_charts, GENERATION_URL)[0]["start"] == yesterday
    state = config_entry.runtime_data.state
    assert state.generation.since == yesterday
    # the window comes from 31 Oct (first generation 05:15, last 18:30): from 07:15 to 21:00
    energy_charts.mock_calls.clear()
    await wait_until(hass, freezer, "2026-11-01T20:05:00+00:00")  # 21:05 Vienna
    times = [
        datetime.fromtimestamp(c["end"], VIENNA).strftime("%H:%M") for c in requests_to(energy_charts, GENERATION_URL)
    ]
    assert times[-1] == "21:00"  # the last one: when the window ends
    energy_charts.mock_calls.clear()
    await wait_until(hass, freezer, "2026-11-01T22:55:00+00:00")  # 23:55 Vienna
    assert requests_to(energy_charts, GENERATION_URL) == []


@pytest.mark.freeze_time("2026-10-15T20:30:00+00:00")  # 22:30 Vienna
async def test_download_times_are_spread(
    hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts, freezer: FrozenDateTimeFactory
) -> None:
    """With the default spread the nightly full download is at 02:00 + this entry's shift of the day."""
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="spread-test", data=dict(config_entry.data), options={CONF_REQUEST_DELAY: 0}
    )
    await setup(hass, entry)
    settings = schedule_settings(entry.options, entry.entry_id)
    moved = jitter(settings, date(2026, 10, 16))
    assert settings.spread == 600 and moved != 0
    slot = datetime.fromisoformat("2026-10-16T02:00:00+02:00").timestamp() + moved
    energy_charts.mock_calls.clear()
    await wait_until(hass, freezer, datetime.fromtimestamp(slot - 30, UTC).isoformat())
    assert requests_to(energy_charts, GENERATION_URL) == []
    await wait_until(hass, freezer, datetime.fromtimestamp(slot + 30, UTC).isoformat())
    assert len(requests_to(energy_charts, GENERATION_URL)) == 1  # the full download, at its shifted time
    assert abs(requests_to(energy_charts, GENERATION_URL)[0]["end"] - slot) <= 30


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
    official = hass.states.get("sensor.referenzmarktwert_pv_official_value_latest")
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

    # a retry while Energy-Charts still asks us to wait: no request, and still no empty sensors
    aioclient_mock.mock_calls.clear()
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert energy_charts_calls(aioclient_mock) == 0


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
    assert hass.states.get("sensor.referenzmarktwert_pv_official_value_latest").state == "unknown"

    energy_charts.clear_requests()
    energy_charts.get(PRICE_URL, json=fixture_json("price_at_2026-10.json"))
    energy_charts.get(GENERATION_URL, json=fixture_json("public_power_at_2026-10.json"))
    energy_charts.get(DEFAULT_ECONTROL_XLSX_URL, content=(FIXTURES / "Referenzmarktwert_Entwicklung.xlsx").read_bytes())
    await wait_until(hass, freezer, "2026-10-31T22:31:00+00:00")  # an hour later, not a day
    assert float(hass.states.get("sensor.referenzmarktwert_pv_official_value_latest").state) == pytest.approx(0.1771)


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
    assert hass.states.get("sensor.referenzmarktwert_pv_official_value_latest").state == "unknown"
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
async def test_no_save_after_unload(
    hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts, hass_storage
) -> None:
    """An update still running when the entry is unloaded (or removed) must not write the file again."""
    await setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    del hass_storage[storage_key(config_entry.entry_id)]
    coordinator.state.official_attempt = coordinator.state.generation_attempt = None  # downloads are due
    with patch.object(coordinator, "_report_deprecation") as report:
        await coordinator._async_update_data()
    assert storage_key(config_entry.entry_id) not in hass_storage
    report.assert_not_called()  # a removed entry's repair notice must not come back


@pytest.mark.freeze_time(NOW)
@pytest.mark.parametrize("version", [STORAGE_VERSION - 1, STORAGE_VERSION + 1])  # older; newer after a downgrade
async def test_stored_data_of_another_version_is_downloaded_again(
    hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts, hass_storage, version: int
) -> None:
    key = storage_key(config_entry.entry_id)
    hass_storage[key] = {"version": version, "minor_version": 1, "key": key, "data": {"x": 1}}
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert requests_to(energy_charts, GENERATION_URL)  # downloaded again
    assert hass_storage[key]["version"] == STORAGE_VERSION  # replaced by the new data


@pytest.mark.freeze_time(NOW)
async def test_requests_are_paused_between_not_after(
    hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts
) -> None:
    """The pause keeps us below Energy-Charts' rate limit; after the last request it only costs time."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=dict(config_entry.data), options={**config_entry.options, CONF_REQUEST_DELAY: 0.25}
    )
    pauses = []
    sleep = asyncio.sleep

    async def record(delay, *args, **kwargs):
        if delay == 0.25:
            pauses.append(delay)
        return await sleep(0, *args, **kwargs)

    with patch("custom_components.referenzmarktwert.coordinator.asyncio.sleep", record):
        await setup(hass, entry)
    assert len(pauses) == energy_charts_calls(energy_charts) - 1  # generation, prices, reference: two pauses


@pytest.mark.freeze_time(NOW)
async def test_diagnostics_redacts_headers(hass: HomeAssistant, energy_charts) -> None:
    from custom_components.referenzmarktwert.const import (
        CONF_ECONTROL_HEADER_NAME,
        CONF_ECONTROL_HEADER_VALUE,
        CONF_ECONTROL_URL,
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
            CONF_ECONTROL_URL: DEFAULT_ECONTROL_XLSX_URL + "?key=SECRET",
        },
        options={"request_delay_seconds": 0},
    )
    await setup(hass, entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["data"][CONF_HEADER_VALUE] == "**REDACTED**"
    assert diagnostics["data"][CONF_ECONTROL_URL] == DEFAULT_ECONTROL_XLSX_URL  # without the query
    assert diagnostics["data"][CONF_ECONTROL_HEADER_VALUE] == "**REDACTED**"
    assert diagnostics["estimate"]["month"] == (2026, 10)
    assert diagnostics["stored"]["generation_since"] == OCTOBER[0]
    # the headers are really sent, each only to its own source
    calls = energy_charts.mock_calls
    assert any(h and h.get("X-Api-Key") == "secret" for _, url, _, h in calls if str(url).startswith(PRICE_URL))
    econtrol = [h for _, url, _, h in calls if str(url).startswith(DEFAULT_ECONTROL_XLSX_URL)]
    assert any(h and h.get("X-Other") == "hidden" for h in econtrol)
    assert not any(h and "X-Other" in h for _, url, _, h in calls if str(url).startswith(PRICE_URL))


@pytest.mark.freeze_time(NOW)
async def test_econtrol_failure_is_retried_after_a_reload(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker, freezer
) -> None:
    """The failed attempt is stored; after a reload (e.g. saving the options) it must still be retried
    every interval, not only a day later."""
    fake_sources(aioclient_mock, b"not available")  # E-Control answers with garbage
    await setup(hass, config_entry)
    assert hass.states.get("sensor.referenzmarktwert_pv_official_value_latest").state == "unknown"
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    aioclient_mock.clear_requests()
    fake_sources(aioclient_mock, econtrol_xlsx())
    await wait_until(hass, freezer, "2026-10-31T22:31:00+00:00")  # an hour later
    assert float(hass.states.get("sensor.referenzmarktwert_pv_official_value_latest").state) == pytest.approx(0.1771)


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
    assert estimate.attributes["previous_month_value"] == pytest.approx(0.1053, abs=0.0001)
    assert estimate.attributes["data_until_prices"] == datetime.fromtimestamp(OCTOBER[0] + 86400, UTC).isoformat()

    # 02:00: the nightly full download includes September (not official yet)
    aioclient_mock.mock_calls.clear()
    await wait_until(hass, freezer, "2026-10-01T00:05:00+00:00")
    assert requests_to(aioclient_mock, GENERATION_URL)[0]["start"] == september[0]


@pytest.mark.freeze_time(NOW)
async def test_deprecation_notice(hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock) -> None:
    """Energy-Charts announces the end of an endpoint with "deprecated": true: a repair notice appears."""
    generation = {**fixture_json("public_power_at_2026-10.json"), "deprecated": True}
    price = {**fixture_json("price_at_2026-10.json"), "deprecated": True}
    aioclient_mock.get(GENERATION_URL, json=generation)
    aioclient_mock.get(PRICE_URL, json=price)
    aioclient_mock.get(DEFAULT_ECONTROL_XLSX_URL, content=econtrol_xlsx())
    await setup(hass, config_entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, deprecation_issue_id(config_entry.entry_id))
    assert issue is not None
    assert issue.translation_key == "energy_charts_deprecated"
    assert issue.learn_more_url == DEFAULT_ENERGY_CHARTS_BASE_URL  # the configured API
    assert config_entry.state is ConfigEntryState.LOADED  # it still works

    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, deprecation_issue_id(config_entry.entry_id)) is None


@pytest.mark.freeze_time(NOW)
async def test_no_deprecation_notice(hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts) -> None:
    await setup(hass, config_entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, deprecation_issue_id(config_entry.entry_id)) is None


@pytest.mark.freeze_time(NOW)
async def test_entity_ids_are_english_in_a_german_home_assistant(
    hass: HomeAssistant, config_entry: MockConfigEntry, energy_charts
) -> None:
    """Names are translated, entity IDs are not (Home Assistant would derive German ones)."""
    hass.config.language = "de"
    await setup(hass, config_entry)
    registry = er.async_get(hass)
    ids = sorted(e.entity_id for e in er.async_entries_for_config_entry(registry, config_entry.entry_id))
    assert ids == [
        "sensor.referenzmarktwert_pv_estimate_running_month",
        "sensor.referenzmarktwert_pv_estimate_running_month_high",
        "sensor.referenzmarktwert_pv_estimate_running_month_low",
        "sensor.referenzmarktwert_pv_expected_error_of_the_estimate",
        "sensor.referenzmarktwert_pv_official_value_latest",
        "sensor.referenzmarktwert_pv_payout_estimate_running_month",
        "sensor.referenzmarktwert_pv_payout_estimate_running_month_high",
        "sensor.referenzmarktwert_pv_payout_estimate_running_month_low",
        "sensor.referenzmarktwert_pv_payout_official_value_latest",
    ]
    state = hass.states.get("sensor.referenzmarktwert_pv_official_value_latest")
    assert state.attributes["friendly_name"] == "Referenzmarktwert PV Offizieller Wert zuletzt veröffentlicht"
