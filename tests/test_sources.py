"""Tests for the data sources.

Parsers run on recorded responses; the HTTP client runs against a local aiohttp test server, so no
network is needed. `test_live_*` talk to the real APIs and only run with RMW_LIVE=1.
"""

import asyncio
import io
import json
import os
import zipfile
from datetime import date
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.referenzmarktwert import sources
from custom_components.referenzmarktwert.sources import (
    EControlConfig,
    EnergyChartsClient,
    EnergyChartsConfig,
    SourceConnectionError,
    SourceDataError,
    SourceRateLimitError,
)

FIXTURES = Path(__file__).parent / "fixtures"
XLSX = FIXTURES / "Referenzmarktwert_Entwicklung.xlsx"


def fixture_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# --- Energy-Charts parsers -----------------------------------------------------------------------


def test_parse_generation() -> None:
    series = sources.parse_generation(fixture_json("public_power_at_2026-10.json"))
    assert len(series) == 2981  # 31 days + 1 h DST + the inclusive end point
    assert all(isinstance(ts, int) for ts, _ in series)


def test_parse_generation_unknown_type() -> None:
    with pytest.raises(SourceDataError, match="'Wind' not in"):
        sources.parse_generation(fixture_json("public_power_at_2026-10.json"), "Wind")


def test_parse_price() -> None:
    series = sources.parse_price(fixture_json("price_at_2026-10.json"))
    assert len(series) == 2981


@pytest.mark.parametrize(
    "payload",
    [
        {"unix_seconds": [1], "price": [1.0], "unit": "EUR / kWh"},  # unit changed
        {"unix_seconds": [1, 2], "price": [1.0], "unit": "EUR / MWh"},  # length mismatch
        {"unix_seconds": [1], "price": ["x"], "unit": "EUR / MWh"},  # not a number
        {"price": [1.0], "unit": "EUR / MWh"},  # no timestamps
        [],
    ],
)
def test_parse_price_rejects_unexpected(payload: object) -> None:
    with pytest.raises(SourceDataError):
        sources.parse_price(payload)


def test_parse_price_keeps_missing_values() -> None:
    assert sources.parse_price({"unix_seconds": [1, 2], "price": [None, 3], "unit": "EUR / MWh"}) == [
        (1, None),
        (2, 3.0),
    ]


# --- E-Control parser ----------------------------------------------------------------------------


def test_parse_econtrol_xlsx() -> None:
    values = sources.parse_econtrol_xlsx(XLSX.read_bytes())
    assert len(values) == 58
    assert min(values) == (2022, 1) and max(values) == (2026, 10)
    oct26 = values[(2026, 10)]
    assert oct26.value == pytest.approx(0.1771)  # 17.71 ct/kWh -> EUR/kWh
    assert oct26.base == pytest.approx(0.2202)
    assert oct26.published == date(2026, 11, 4)
    assert values[(2026, 9)].value == pytest.approx(0.1053)


def test_parse_econtrol_other_column() -> None:
    values = sources.parse_econtrol_xlsx(XLSX.read_bytes(), EControlConfig(value_column="C", base_column=None))
    assert values[(2022, 1)].value == pytest.approx(0.1851)  # wind
    assert values[(2022, 1)].base is None


def make_xlsx(rows: list[list[str | float]]) -> bytes:
    """A minimal xlsx with inline strings and numbers, to test edge cases."""
    cells = []
    for r, row in enumerate(rows, start=1):
        xml = []
        for c, value in zip("ABCDEF", row, strict=False):
            if isinstance(value, str):
                xml.append(f'<c r="{c}{r}" t="inlineStr"><is><t>{value}</t></is></c>')
            else:
                xml.append(f'<c r="{c}{r}"><v>{value}</v></c>')
        cells.append(f'<row r="{r}">{"".join(xml)}</row>')
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets><sheet name="x" sheetId="1" r:id="rId9"/></sheets>'
            "</workbook>",
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId9" Target="worksheets/data.xml"/></Relationships>',
        )
        z.writestr(
            "xl/worksheets/data.xml", f'<worksheet xmlns="{main}"><sheetData>{"".join(cells)}</sheetData></worksheet>'
        )
    return buffer.getvalue()


def test_parse_xlsx_eur_unit_and_unpublished_month() -> None:
    data = make_xlsx(
        [["Monat", "", "", "PV", "Base"], ["", "", "", "EUR/kWh", "EUR/kWh"], [46296, 0, 0, 0.17, 0.2], [46327]]
    )
    values = sources.parse_econtrol_xlsx(data)
    assert list(values) == [(2026, 10)]  # 46327 (Nov) has no value yet
    assert values[(2026, 10)].value == 0.17


def test_parse_xlsx_unknown_unit() -> None:
    with pytest.raises(SourceDataError, match="unit"):
        sources.parse_econtrol_xlsx(make_xlsx([["Monat", "", "", "MWh"], [46296, 0, 0, 1.0]]))


@pytest.mark.parametrize("data", [b"not a zip", make_xlsx([["only", "text"]])])
def test_parse_xlsx_rejects_garbage(data: bytes) -> None:
    with pytest.raises(SourceDataError):
        sources.parse_econtrol_xlsx(data)


# --- HTTP client against a local test server -----------------------------------------------------


@pytest.fixture
async def server(socket_enabled: None):
    """Serves the fixtures like the real APIs; `server.requests` records the query parameters.

    Home Assistant's test plugin blocks sockets; `socket_enabled` allows them for these tests only.
    """
    requests: list[tuple[str, dict[str, str]]] = []

    async def handle(request: web.Request) -> web.Response:
        requests.append((request.path, dict(request.query)))
        if request.query.get("bzn") == "LIMIT":
            return web.Response(status=429, headers={"Retry-After": "7"})
        if request.query.get("bzn") == "BROKEN":
            return web.Response(status=500)
        if request.path.endswith("/price"):
            return web.json_response(fixture_json("price_at_2026-10.json"))
        if request.path.endswith("/public_power"):
            return web.json_response(fixture_json("public_power_at_2026-10.json"))
        if request.path.endswith(".xlsx"):
            return web.Response(body=XLSX.read_bytes())
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/{tail:.*}", handle)
    test_server = TestServer(app)
    await test_server.start_server()
    test_server.requests = requests
    yield test_server
    await test_server.close()


async def test_client_fetches_and_sends_config(server: TestServer) -> None:
    config = EnergyChartsConfig(base_url=str(server.make_url("/api")), country="xx", bidding_zone="YY")
    async with aiohttp.ClientSession() as session:
        client = EnergyChartsClient(session, config)
        assert len(await client.price(1, 2)) == 2981
        assert len(await client.generation(1, 2)) == 2981
    assert server.requests == [
        ("/api/price", {"bzn": "YY", "start": "1", "end": "2"}),
        ("/api/public_power", {"country": "xx", "start": "1", "end": "2"}),
    ]


async def test_client_rate_limit(server: TestServer) -> None:
    config = EnergyChartsConfig(base_url=str(server.make_url("")), bidding_zone="LIMIT")
    async with aiohttp.ClientSession() as session:
        with pytest.raises(SourceRateLimitError) as caught:
            await EnergyChartsClient(session, config).price(1, 2)
    assert caught.value.retry_after == 7
    assert caught.value.status == 429


async def test_client_http_error(server: TestServer) -> None:
    config = EnergyChartsConfig(base_url=str(server.make_url("")), bidding_zone="BROKEN")
    async with aiohttp.ClientSession() as session:
        with pytest.raises(SourceConnectionError, match="HTTP 500") as caught:
            await EnergyChartsClient(session, config).price(1, 2)
    assert caught.value.status == 500


async def test_client_connection_refused(socket_enabled: None) -> None:
    config = EnergyChartsConfig(base_url="http://127.0.0.1:9", timeout=2)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(SourceConnectionError):
            await EnergyChartsClient(session, config).price(1, 2)


async def test_fetch_econtrol(server: TestServer) -> None:
    async with aiohttp.ClientSession() as session:
        values = await sources.fetch_econtrol(session, EControlConfig(url=str(server.make_url("/x.xlsx"))))
    assert values[(2026, 10)].value == pytest.approx(0.1771)


# --- Live (opt-in) -------------------------------------------------------------------------------

live = pytest.mark.skipif(os.environ.get("RMW_LIVE") != "1", reason="set RMW_LIVE=1 to call the real APIs")


@pytest.mark.live
@live
async def test_live_energy_charts(internet: None) -> None:
    start, end = 1759269600, 1759273200  # 2025-10-01 00:00..01:00 Vienna
    async with aiohttp.ClientSession() as session:
        client = EnergyChartsClient(session)
        price = await client.price(start, end)
        generation = await client.generation(start, end)
    assert len(price) == 5 and price[0][0] == start
    assert len(generation) == 5


@pytest.mark.live
@live
async def test_live_econtrol(internet: None) -> None:
    async with aiohttp.ClientSession() as session:
        values = await sources.fetch_econtrol(session)
    assert values[(2026, 10)].value == pytest.approx(0.1771)


def test_parse_xlsx_invalid_xml_becomes_source_error() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z, zipfile.ZipFile(XLSX) as good:
        for name in good.namelist():
            z.writestr(name, b"<broken" if name == "xl/worksheets/sheet1.xml" else good.read(name))
    with pytest.raises(SourceDataError, match="damaged"):
        sources.parse_econtrol_xlsx(buffer.getvalue())


def test_parse_xlsx_bad_shared_string_index() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z, zipfile.ZipFile(XLSX) as good:
        for name in good.namelist():
            data = good.read(name)
            if name == "xl/worksheets/sheet1.xml":
                data = data.replace(b't="s"><v>4</v>', b't="s"><v>999</v>')
            z.writestr(name, data)
    with pytest.raises(SourceDataError):
        sources.parse_econtrol_xlsx(buffer.getvalue())


async def test_client_no_content_is_empty(aioclient_mock) -> None:
    """Energy-Charts answers 404 'no content available' for a window without data (month start)."""
    aioclient_mock.get("https://ec.test/price", status=404, text="no content available")
    aioclient_mock.get("https://ec.test/public_power", status=404, text="Not Found")
    session = aioclient_mock.create_session(asyncio.get_running_loop())
    client = EnergyChartsClient(session, EnergyChartsConfig(base_url="https://ec.test"))
    assert await client.price(1, 2) == []
    with pytest.raises(SourceConnectionError) as caught:  # a wrong path stays an error
        await client.generation(1, 2)
    assert caught.value.status == 404
    await session.close()
