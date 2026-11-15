"""Coordinator: fetches Energy-Charts and E-Control data, caches it, computes the estimate.

One coordinator per config entry. Home Assistant calls `_async_update_data` every update interval;
all entities read `coordinator.data`. Requests are sent one after another with a small pause, and
whatever does not change (last year's reference month, a recomputed previous month, the official
table) is cached, so a normal update costs two Energy-Charts requests.
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import calc
from .calc import Month
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
    OFFICIAL_EARLY_DAYS,
    PriorMethod,
)
from .estimate import Estimate, MonthData, estimate_month, recompute_month
from .sources import (
    EControlConfig,
    EnergyChartsClient,
    EnergyChartsConfig,
    OfficialValue,
    SourceError,
    SourceRateLimitError,
    fetch_econtrol,
)

if TYPE_CHECKING:
    from . import ReferenzmarktwertConfigEntry

_LOGGER = logging.getLogger(__package__)

RECOMPUTE_MAX_AGE = timedelta(days=1)
PRICE_LOOKAHEAD = 2 * 86400  # ask for prices until the day after tomorrow; the API returns what exists


def energy_charts_config(data: Mapping[str, Any]) -> EnergyChartsConfig:
    """Energy-Charts settings from a config entry's data (missing keys: defaults)."""
    d = {**DEFAULT_DATA, **data}
    headers = {d[CONF_HEADER_NAME]: d[CONF_HEADER_VALUE]} if d[CONF_HEADER_NAME] else {}
    return EnergyChartsConfig(
        base_url=d[CONF_ENERGY_CHARTS_URL],
        generation_path=d[CONF_GENERATION_PATH],
        price_path=d[CONF_PRICE_PATH],
        country=d[CONF_COUNTRY],
        bidding_zone=d[CONF_BIDDING_ZONE],
        production_type=d[CONF_PRODUCTION_TYPE],
        headers=headers,
        timeout=float(d[CONF_TIMEOUT]),
    )


def econtrol_config(data: Mapping[str, Any]) -> EControlConfig:
    """E-Control settings from a config entry's data; an empty column means "not used"."""
    d = {**DEFAULT_DATA, **data}
    return EControlConfig(
        url=d[CONF_ECONTROL_URL],
        month_column=d[CONF_ECONTROL_MONTH_COLUMN],
        value_column=d[CONF_ECONTROL_VALUE_COLUMN],
        base_column=d[CONF_ECONTROL_BASE_COLUMN] or None,
        published_column=d[CONF_ECONTROL_PUBLISHED_COLUMN] or None,
        timeout=float(d[CONF_TIMEOUT]),
    )


@dataclass(frozen=True, slots=True)
class ReferenzmarktwertData:
    """What the entities show: the running month's estimate and the official table."""

    estimate: Estimate
    official: Mapping[Month, OfficialValue] = field(default_factory=dict)
    official_checked: datetime | None = None  # last successful download of the E-Control file
    deduction: float = 0.0

    @property
    def latest_official(self) -> OfficialValue | None:
        return self.official[max(self.official)] if self.official else None


class ReferenzmarktwertCoordinator(DataUpdateCoordinator[ReferenzmarktwertData]):
    """Fetches and caches the data of one config entry."""

    config_entry: ReferenzmarktwertConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ReferenzmarktwertConfigEntry) -> None:
        self._options = {**DEFAULT_OPTIONS, **entry.options}
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=float(self._options[CONF_UPDATE_INTERVAL])),
        )
        session = async_get_clientsession(hass)
        self._session = session
        self._energy_charts = EnergyChartsClient(session, energy_charts_config(entry.data))
        self._econtrol = econtrol_config(entry.data)
        self._tz_name: str = {**DEFAULT_DATA, **entry.data}[CONF_TIME_ZONE]
        self._tz: tzinfo | None = None
        self._period = int(self._options[CONF_AGGREGATION_SECONDS])
        self._method = PriorMethod(self._options[CONF_PRIOR_METHOD])
        self._delay = float(self._options[CONF_REQUEST_DELAY])
        # caches
        self._official: dict[Month, OfficialValue] = {}
        self._official_checked: datetime | None = None
        self._official_attempted: datetime | None = None
        self._official_failing = False
        self._reference: dict[Month, MonthData | None] = {}
        # previous month recomputed from Energy-Charts: (value, base), fetched at
        self._recomputed: dict[Month, tuple[tuple[float | None, float | None], datetime]] = {}

    async def _async_setup(self) -> None:
        """Runs once before the first update (loading a time zone reads a file: do it async)."""
        self._tz = await dt_util.async_get_time_zone(self._tz_name)
        if self._tz is None:
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="invalid_time_zone",
                translation_placeholders={"time_zone": self._tz_name},
            )

    async def _async_update_data(self) -> ReferenzmarktwertData:
        assert self._tz is not None  # set in _async_setup
        now = dt_util.utcnow()
        month = calc.month_of(now.timestamp(), self._tz)
        await self._refresh_official(now, month)
        try:
            current = await self._fetch_month(month, until=int(now.timestamp()))
            reference = await self._reference_month(calc.shift_month(month, -12))
            previous = calc.shift_month(month, -1)
            recomputed = None if previous in self._official else await self._recompute(previous)
        except SourceRateLimitError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="rate_limited",
                translation_placeholders={"error": str(err)},
                retry_after=err.retry_after,
            ) from err
        except SourceError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="energy_charts_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        estimate = estimate_month(
            now.timestamp(),
            self._tz,
            current,
            reference,
            {m: v.value for m, v in self._official.items()},
            {m: v.base for m, v in self._official.items() if v.base is not None},
            recomputed,
            self._method,
            self._period,
        )
        return ReferenzmarktwertData(
            estimate=estimate,
            official=dict(self._official),
            official_checked=self._official_checked,
            deduction=float(self._options[CONF_DEDUCTION]),
        )

    # --- E-Control -------------------------------------------------------------------------------

    def _official_due(self, now: datetime, month: Month) -> bool:
        """Download the file daily; every few hours while last month's value is awaited; on every
        update while the download fails (one failed attempt at startup must not hide it for a day)."""
        if self._official_attempted is None or self._official_failing:
            return True  # first time, or retry on every update while it fails
        assert self._tz is not None
        awaited = calc.shift_month(month, -1) not in self._official
        early = awaited and now.astimezone(self._tz).day <= OFFICIAL_EARLY_DAYS
        hours = self._options[CONF_OFFICIAL_EARLY_INTERVAL if early else CONF_OFFICIAL_INTERVAL]
        return now - self._official_attempted >= timedelta(hours=float(hours))

    async def _refresh_official(self, now: datetime, month: Month) -> None:
        """Update the official table if due. A failure keeps the last table (it rarely changes)."""
        if not self._official_due(now, month):
            return
        self._official_attempted = now
        try:
            official = await fetch_econtrol(self._session, self._econtrol)
        except SourceError as err:
            if not self._official_failing:  # log once when it starts failing ...
                _LOGGER.warning("E-Control file not available, keeping the last values: %s", err)
                self._official_failing = True
            return
        if self._official_failing:  # ... and once when it works again
            _LOGGER.info("E-Control file available again")
            self._official_failing = False
        self._official = official
        self._official_checked = now
        self._recomputed = {m: v for m, v in self._recomputed.items() if m not in official}

    # --- Energy-Charts ---------------------------------------------------------------------------

    async def _pause(self) -> None:
        if self._delay > 0:
            await asyncio.sleep(self._delay)

    async def _fetch_month(self, month: Month, until: int | None = None) -> MonthData:
        """Generation and prices of a month; for the running month up to now (prices: ahead)."""
        assert self._tz is not None
        start, end = calc.month_bounds(month, self._tz)
        gen_end = min(end, until) if until is not None else end
        price_end = min(end, until + PRICE_LOOKAHEAD) if until is not None else end
        generation = await self._energy_charts.generation(start, gen_end)
        await self._pause()
        price = await self._energy_charts.price(start, price_end)
        await self._pause()
        return MonthData(generation, price)

    async def _reference_month(self, month: Month) -> MonthData | None:
        """Last year's generation of the same month, fetched once (only generation is needed)."""
        if month not in self._reference:
            assert self._tz is not None
            start, end = calc.month_bounds(month, self._tz)
            generation = await self._energy_charts.generation(start, end)
            await self._pause()
            self._reference = {month: MonthData(generation, [])}  # keep only the current one
        return self._reference[month]

    async def _recompute(self, month: Month) -> tuple[float | None, float | None]:
        """The previous month recomputed from Energy-Charts, until E-Control publishes it.

        Refreshed daily: right after the month change its last hours may still be missing.
        """
        cached = self._recomputed.get(month)
        now = dt_util.utcnow()
        if cached is None or now - cached[1] >= RECOMPUTE_MAX_AGE:
            assert self._tz is not None
            value = recompute_month(await self._fetch_month(month), month, self._tz, self._period)
            self._recomputed[month] = (value, now)
        return self._recomputed[month][0]
