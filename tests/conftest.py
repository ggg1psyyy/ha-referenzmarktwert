"""Shared test fixtures."""

import importlib.util
import json
import time
from datetime import datetime, timedelta
from datetime import time as clock
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_socket
from astral.geocoder import database, lookup
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse
from yarl import URL

from custom_components.referenzmarktwert import calc, sources
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
VIENNA = ZoneInfo("Europe/Vienna")
VIENNA_CITY = lookup("Vienna", database())  # a city's public coordinates, from astral (no location of anyone)
LAST_MODIFIED = "Thu, 05 Nov 2026 14:54:13 GMT"
GENERATION_LAG = 2 * 3600  # Energy-Charts publishes generation about 2 h late


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


@pytest.fixture
async def vienna(hass):
    """Home Assistant's home in Vienna (astral's city database; the test default is San Diego)."""
    city = VIENNA_CITY
    hass.config.latitude, hass.config.longitude = city.latitude, city.longitude
    await hass.config.async_set_time_zone("Europe/Vienna")


def _recorded(names: list[str], parse) -> list[tuple[int, float | None]]:
    return sorted(point for name in names for point in parse(fixture_json(name)))


def econtrol_xlsx(until: tuple[int, int] | None = None) -> bytes:
    """The E-Control test file, optionally only with the months up to `until` (as if not yet published)."""
    path = Path(__file__).parents[1] / "tools/make_econtrol_fixture.py"
    spec = importlib.util.spec_from_file_location("make_econtrol_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lines = [line for line in module.VALUES.split("\n") if line.strip()]
    if until is not None:
        lines = [line for line in lines if line[:7] <= f"{until[0]}-{until[1]:02d}"]
    return module.build("\n".join(lines))


@pytest.fixture
def energy_charts(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Energy-Charts and E-Control, see `fake_sources`."""
    return fake_sources(aioclient_mock, (FIXTURES / "Referenzmarktwert_Entwicklung.xlsx").read_bytes())


def fake_sources(aioclient_mock: AiohttpClientMocker, xlsx: bytes) -> AiohttpClientMocker:
    """Energy-Charts as it behaves, from recorded data (Oct 2025, Sep + Oct 2026).

    Answers exactly the requested window (`end` inclusive) with what is published at the (frozen)
    current time: generation until ~2 h ago, day-ahead prices until the end of today, or of tomorrow
    after 13:00 Vienna time. An empty window is "404 no content available", as the real API does.
    E-Control serves the history file with a Last-Modified header.
    """
    generation = _recorded(
        ["public_power_at_2025-10.json", "public_power_at_2026-09.json", "public_power_at_2026-10.json"],
        sources.parse_generation,
    )
    price = _recorded(["price_at_2026-09.json", "price_at_2026-10.json"], sources.parse_price)

    async def answer(method: str, url: URL, data) -> AiohttpClientMockResponse:
        start, end = int(url.query["start"]), int(url.query["end"])
        now = time.time()
        if url.path.endswith("/public_power"):
            until = min(end, now - GENERATION_LAG)
            points = [(ts, v) for ts, v in generation if start <= ts <= until]
            body = {
                "unix_seconds": [ts for ts, _ in points],
                "production_types": [{"name": "Solar", "data": [v for _, v in points]}],
                "deprecated": False,
            }
        else:
            local = datetime.fromtimestamp(now, VIENNA)
            days = 2 if local.hour >= 13 else 1
            published = datetime.combine(local.date() + timedelta(days=days), clock(), VIENNA).timestamp()
            points = [(ts, v) for ts, v in price if start <= ts <= min(end, published - 900)]
            body = {
                "unix_seconds": [ts for ts, _ in points],
                "price": [v for _, v in points],
                "unit": "EUR / MWh",
                "deprecated": False,
            }
        if not points:
            return AiohttpClientMockResponse(method, url, status=404, text="no content available")
        return AiohttpClientMockResponse(method, url, json=body)

    aioclient_mock.get(GENERATION_URL, side_effect=answer)
    aioclient_mock.get(PRICE_URL, side_effect=answer)
    aioclient_mock.get(DEFAULT_ECONTROL_XLSX_URL, content=xlsx, headers={"Last-Modified": LAST_MODIFIED})
    return aioclient_mock


def requests_to(mock: AiohttpClientMocker, base: str) -> list[dict[str, int]]:
    """The query parameters (start, end) of every request to `base`, in order."""
    return [
        {key: int(value) for key, value in url.query.items() if key in ("start", "end")}
        for _, url, _, _ in mock.mock_calls
        if str(url).startswith(base)
    ]
