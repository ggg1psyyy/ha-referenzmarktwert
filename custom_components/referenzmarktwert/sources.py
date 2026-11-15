"""Data sources: Energy-Charts (generation, day-ahead prices) and E-Control (official monthly values).

No Home Assistant imports. Network access goes through an `aiohttp.ClientSession` that the caller
passes in — inside Home Assistant that is the shared session (`async_get_clientsession(hass)`),
in tests a local test server. Parsing is separated from fetching and works on plain data.

Errors are reported as `SourceError` subclasses so the caller can decide what to do (retry later,
show "unavailable", ...).
"""

import io
import json
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import aiohttp

from .calc import Month
from .const import (
    DEFAULT_BIDDING_ZONE,
    DEFAULT_COUNTRY,
    DEFAULT_ECONTROL_BASE_COLUMN,
    DEFAULT_ECONTROL_COLUMN,
    DEFAULT_ECONTROL_MONTH_COLUMN,
    DEFAULT_ECONTROL_PUBLISHED_COLUMN,
    DEFAULT_ECONTROL_XLSX_URL,
    DEFAULT_ENERGY_CHARTS_BASE_URL,
    DEFAULT_ENERGY_CHARTS_GENERATION_PATH,
    DEFAULT_ENERGY_CHARTS_PRICE_PATH,
    DEFAULT_PRODUCTION_TYPE,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
)

# --- Errors --------------------------------------------------------------------------------------


class SourceError(Exception):
    """Base class: data could not be obtained."""


class SourceConnectionError(SourceError):
    """Network problem, timeout or unexpected HTTP status."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # HTTP status, if the server answered at all


class SourceRateLimitError(SourceConnectionError):
    """HTTP 429: the server asks us to slow down."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after  # seconds, if the server said so


class SourceDataError(SourceError):
    """The response arrived but does not look as expected (format or unit changed?)."""


# --- HTTP helper ---------------------------------------------------------------------------------


async def _get(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: Mapping[str, str | int] | None,
    headers: Mapping[str, str],
    timeout: float,
) -> bytes:
    """GET `url` and return the body; map every failure to a SourceError."""
    try:
        async with session.get(
            url,
            params=params,
            headers=dict(headers),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if response.status == 429:
                raise SourceRateLimitError(f"{url}: rate limited (HTTP 429)", _retry_after(response))
            if response.status != 200:
                # 4xx bodies explain what was wrong (e.g. an unknown bidding zone): keep a short excerpt
                detail = ""
                if 400 <= response.status < 500:
                    detail = ": " + (await response.text(errors="replace"))[:200]
                raise SourceConnectionError(f"{url}: HTTP {response.status}{detail}", response.status)
            return await response.read()
    except TimeoutError as err:
        raise SourceConnectionError(f"{url}: timeout after {timeout} s") from err
    except aiohttp.ClientError as err:
        raise SourceConnectionError(f"{url}: {err}") from err


def _retry_after(response: aiohttp.ClientResponse) -> float | None:
    try:
        return float(response.headers["Retry-After"])
    except KeyError, ValueError:
        return None


# --- Energy-Charts -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnergyChartsConfig:
    """Everything about the Energy-Charts API that a user may want to change."""

    base_url: str = DEFAULT_ENERGY_CHARTS_BASE_URL
    generation_path: str = DEFAULT_ENERGY_CHARTS_GENERATION_PATH
    price_path: str = DEFAULT_ENERGY_CHARTS_PRICE_PATH
    country: str = DEFAULT_COUNTRY
    bidding_zone: str = DEFAULT_BIDDING_ZONE
    production_type: str = DEFAULT_PRODUCTION_TYPE
    headers: Mapping[str, str] = field(default_factory=dict)  # e.g. an API key, if ever needed
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS


def _json(body: bytes, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as err:
        raise SourceDataError(f"{what}: response is not JSON") from err


def _paired(payload: Any, values: Any, what: str) -> list[tuple[int, float | None]]:
    """Zip `unix_seconds` with a value list, validating shapes and types."""
    stamps = payload.get("unix_seconds") if isinstance(payload, dict) else None
    if not isinstance(stamps, list) or not isinstance(values, list) or len(stamps) != len(values):
        raise SourceDataError(f"{what}: unix_seconds/values missing or of different length")
    try:
        return [(int(ts), None if v is None else float(v)) for ts, v in zip(stamps, values, strict=True)]
    except (TypeError, ValueError) as err:
        raise SourceDataError(f"{what}: non-numeric value") from err


def parse_generation(payload: Any, production_type: str = DEFAULT_PRODUCTION_TYPE) -> list[tuple[int, float | None]]:
    """Extract one production type (MW per quarter-hour) from a `public_power` response."""
    types = payload.get("production_types") if isinstance(payload, dict) else None
    if not isinstance(types, list):
        raise SourceDataError("public_power: no production_types")
    for entry in types:
        if isinstance(entry, dict) and entry.get("name") == production_type:
            return _paired(payload, entry.get("data"), "public_power")
    names = [e.get("name") for e in types if isinstance(e, dict)]
    raise SourceDataError(f"public_power: production type {production_type!r} not in {names}")


def parse_price(payload: Any) -> list[tuple[int, float | None]]:
    """Extract the day-ahead prices (EUR/MWh) from a `price` response; the unit is checked."""
    if not isinstance(payload, dict):
        raise SourceDataError("price: unexpected response")
    unit = str(payload.get("unit", "")).replace(" ", "")
    if unit != "EUR/MWh":
        raise SourceDataError(f"price: unexpected unit {payload.get('unit')!r}, expected EUR/MWh")
    return _paired(payload, payload.get("price"), "price")


NO_CONTENT = "no content available"  # Energy-Charts' 404 body for a window without data


class EnergyChartsClient:
    """Async client for the two Energy-Charts endpoints we need."""

    def __init__(self, session: aiohttp.ClientSession, config: EnergyChartsConfig | None = None) -> None:
        self._session = session
        self.config = config or EnergyChartsConfig()

    async def _fetch(self, path: str, params: dict[str, str | int], what: str) -> Any | None:
        """The JSON answer, or None if the window holds no data yet (HTTP 404 "no content available")."""
        try:
            body = await _get(
                self._session,
                self.config.base_url.rstrip("/") + path,
                params=params,
                headers=self.config.headers,
                timeout=self.config.timeout,
            )
        except SourceConnectionError as err:
            # e.g. the first hours of a month: not an error. Any other 404 (wrong path) still is.
            if err.status == 404 and NO_CONTENT in str(err):
                return None
            raise
        return _json(body, what)

    async def generation(self, start: int, end: int) -> list[tuple[int, float | None]]:
        """Generation of the configured production type, quarter-hourly, MW. `end` is inclusive."""
        payload = await self._fetch(
            self.config.generation_path,
            {"country": self.config.country, "start": start, "end": end},
            "public_power",
        )
        return [] if payload is None else parse_generation(payload, self.config.production_type)

    async def price(self, start: int, end: int) -> list[tuple[int, float | None]]:
        """Day-ahead prices of the configured bidding zone, quarter-hourly, EUR/MWh. `end` inclusive."""
        payload = await self._fetch(
            self.config.price_path,
            {"bzn": self.config.bidding_zone, "start": start, "end": end},
            "price",
        )
        return [] if payload is None else parse_price(payload)


# --- E-Control -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EControlConfig:
    """Location and layout of E-Control's history file."""

    url: str = DEFAULT_ECONTROL_XLSX_URL
    month_column: str = DEFAULT_ECONTROL_MONTH_COLUMN
    value_column: str = DEFAULT_ECONTROL_COLUMN
    base_column: str | None = DEFAULT_ECONTROL_BASE_COLUMN
    published_column: str | None = DEFAULT_ECONTROL_PUBLISHED_COLUMN
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class OfficialValue:
    """One month of E-Control's table, converted to EUR/kWh."""

    month: Month
    value: float  # the technology's reference market value, EUR/kWh
    base: float | None  # day-ahead base price (Referenzmarktpreis), EUR/kWh
    published: date | None


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_MAX_XML_BYTES = 20 * 1024 * 1024  # refuse "zip bombs"; the real file is ~15 kB
_UNIT_FACTORS = {"cent/kwh": 0.01, "ct/kwh": 0.01, "eur/kwh": 1.0, "€/kwh": 1.0}


def _excel_date(serial: float) -> date:
    """Excel stores dates as days since 1899-12-30 (the epoch absorbs Lotus 1-2-3's leap-year bug)."""
    return date(1899, 12, 30) + timedelta(days=int(serial))


def _read_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        info = archive.getinfo(name)
    except KeyError as err:
        raise SourceDataError(f"xlsx: {name} missing") from err
    if info.file_size > _MAX_XML_BYTES:
        raise SourceDataError(f"xlsx: {name} is suspiciously large")
    return ET.fromstring(archive.read(info))


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    """Resolve the first worksheet via workbook.xml and its relationships (don't guess sheet1.xml)."""
    workbook = _read_xml(archive, "xl/workbook.xml")
    sheet = workbook.find("m:sheets/m:sheet", _NS)
    if sheet is None:
        raise SourceDataError("xlsx: no worksheet")
    rel_id = sheet.get(_REL_NS)
    rels = _read_xml(archive, "xl/_rels/workbook.xml.rels")
    for rel in rels:
        if rel.get("Id") == rel_id:
            target = rel.get("Target", "")
            return target.lstrip("/") if target.startswith("/") else "xl/" + target
    raise SourceDataError("xlsx: worksheet relationship not found")


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _read_xml(archive, "xl/sharedStrings.xml")
    # A shared string may consist of several formatted runs; join all their <t> texts.
    return ["".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")) for si in root.findall("m:si", _NS)]


def _rows(data: bytes) -> list[dict[str, str]]:
    """All rows of the first worksheet as {column letter: cell text}."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as err:
        raise SourceDataError("xlsx: not a valid xlsx (zip) file") from err
    with archive:
        strings = _shared_strings(archive)
        sheet = _read_xml(archive, _first_sheet_path(archive))
    rows = []
    for row in sheet.iterfind("m:sheetData/m:row", _NS):
        cells: dict[str, str] = {}
        for cell in row.iterfind("m:c", _NS):
            column = "".join(ch for ch in cell.get("r", "") if ch.isalpha())
            kind = cell.get("t")
            if kind == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(f"{{{_NS['m']}}}t"))
            else:
                v = cell.find("m:v", _NS)
                if v is None or v.text is None:
                    continue
                text = strings[int(v.text)] if kind == "s" else v.text
            cells[column] = text
        rows.append(cells)
    return rows


def _number(text: str | None) -> float | None:
    """The cell text as float, or None if empty / not numeric."""
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def _excel_date_or_none(text: str | None) -> date | None:
    serial = _number(text)
    return _excel_date(serial) if serial is not None else None


# What a damaged or unexpected file can raise below; all of it becomes SourceDataError.
_XLSX_ERRORS = (ET.ParseError, zipfile.BadZipFile, EOFError, KeyError, IndexError, ValueError, OverflowError)


def parse_econtrol_xlsx(data: bytes, config: EControlConfig | None = None) -> dict[Month, OfficialValue]:
    """Parse E-Control's history file; every failure is raised as SourceDataError."""
    try:
        return _parse_econtrol_xlsx(data, config or EControlConfig())
    except SourceDataError:
        raise
    except _XLSX_ERRORS as err:
        raise SourceDataError(f"xlsx: damaged or unexpected file ({err!r})") from err


def _parse_econtrol_xlsx(data: bytes, cfg: EControlConfig) -> dict[Month, OfficialValue]:
    """Parse E-Control's `Referenzmarktwert_Entwicklung.xlsx`.

    Header rows are skipped; the unit is read from the header cell of the value column (e.g.
    "Cent/kWh") and every value is converted to EUR/kWh. Rows without a numeric month (footer) are
    ignored, as are months without a value yet.
    """
    factor: float | None = None
    result: dict[Month, OfficialValue] = {}
    for cells in _rows(data):
        first_day = _excel_date_or_none(cells.get(cfg.month_column))
        if first_day is None:
            if not result:  # still in the header: look for the unit
                unit = cells.get(cfg.value_column, "").replace(" ", "").lower()
                factor = _UNIT_FACTORS.get(unit, factor)
            continue
        if factor is None:
            raise SourceDataError("xlsx: no known unit (e.g. 'Cent/kWh') above the data")
        value = _number(cells.get(cfg.value_column))
        if value is None:
            continue
        base = _number(cells.get(cfg.base_column)) if cfg.base_column else None
        month = (first_day.year, first_day.month)
        result[month] = OfficialValue(
            month=month,
            value=value * factor,
            base=base * factor if base is not None else None,
            published=_excel_date_or_none(cells.get(cfg.published_column)) if cfg.published_column else None,
        )
    if not result:
        raise SourceDataError("xlsx: no monthly values found")
    return result


async def fetch_econtrol(
    session: aiohttp.ClientSession, config: EControlConfig | None = None
) -> dict[Month, OfficialValue]:
    """Download and parse the E-Control history file."""
    cfg = config or EControlConfig()
    body = await _get(session, cfg.url, params=None, headers=cfg.headers, timeout=cfg.timeout)
    return parse_econtrol_xlsx(body, cfg)
