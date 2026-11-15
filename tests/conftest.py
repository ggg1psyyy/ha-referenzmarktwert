"""Shared test fixtures."""

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_socket
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.referenzmarktwert import calc
from custom_components.referenzmarktwert.const import (
    CONF_REQUEST_DELAY,
    DEFAULT_DATA,
    DEFAULT_ECONTROL_XLSX_URL,
    DEFAULT_ENERGY_CHARTS_BASE_URL,
    DEFAULT_OPTIONS,
    DOMAIN,
)

FIXTURES = Path(__file__).parent / "fixtures"
PRICE_URL = f"{DEFAULT_ENERGY_CHARTS_BASE_URL}/price"
GENERATION_URL = f"{DEFAULT_ENERGY_CHARTS_BASE_URL}/public_power"


def fixture_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def internet() -> None:
    """Allow real network access (only for the opt-in live tests).

    Home Assistant's test plugin blocks sockets and allows only 127.0.0.1 for every test; this lifts
    both restrictions for the requesting test (they are set up again before the next one).
    """
    pytest_socket.enable_socket()
    pytest_socket._remove_restrictions()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load integrations from custom_components/ in every test."""


@pytest.fixture
def mock_sources(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Energy-Charts and E-Control answered from recorded data: October 2026, and October 2025 for the
    reference month (registered first: the first matching mock answers)."""
    reference_start, _ = calc.month_bounds((2025, 10), ZoneInfo("Europe/Vienna"))
    aioclient_mock.get(f"{GENERATION_URL}?start={reference_start}", json=fixture_json("public_power_at_2025-10.json"))
    aioclient_mock.get(PRICE_URL, json=fixture_json("price_at_2026-10.json"))
    aioclient_mock.get(GENERATION_URL, json=fixture_json("public_power_at_2026-10.json"))
    aioclient_mock.get(
        DEFAULT_ECONTROL_XLSX_URL, content=(FIXTURES / "Referenzmarktwert_Entwicklung.xlsx").read_bytes()
    )
    return aioclient_mock


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Referenzmarktwert PV",
        unique_id="at_at_solar_d",
        data=dict(DEFAULT_DATA),
        options={**DEFAULT_OPTIONS, CONF_REQUEST_DELAY: 0},
    )
