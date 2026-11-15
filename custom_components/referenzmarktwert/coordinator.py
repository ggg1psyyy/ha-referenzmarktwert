"""Coordinator: downloads Energy-Charts and E-Control data when due, keeps it, computes the estimate.

One coordinator per config entry. `schedule.plan()` decides what is due and when the next thing
becomes due; the coordinator wakes up exactly then (`update_interval` is set after every run).
Downloads are merged into a `CacheState`, which is saved with Home Assistant's storage helper, so
a restart or reload costs no downloads. Requests are sent one after another with a small pause.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store, UnsupportedStorageVersionError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import calc, schedule
from .cache import STORAGE_VERSION, CacheState, SeriesCache
from .calc import Month
from .const import (
    CONF_AGGREGATION_SECONDS,
    CONF_BIDDING_ZONE,
    CONF_COUNTRY,
    CONF_DEDUCTION,
    CONF_ECONTROL_BASE_COLUMN,
    CONF_ECONTROL_HEADER_NAME,
    CONF_ECONTROL_HEADER_VALUE,
    CONF_ECONTROL_MONTH_COLUMN,
    CONF_ECONTROL_PUBLISHED_COLUMN,
    CONF_ECONTROL_URL,
    CONF_ECONTROL_VALUE_COLUMN,
    CONF_END_OFFSET,
    CONF_ENERGY_CHARTS_URL,
    CONF_FULL_REFRESH_TIME,
    CONF_GENERATION_PATH,
    CONF_HEADER_NAME,
    CONF_HEADER_VALUE,
    CONF_OFFICIAL_EARLY_DAYS,
    CONF_OFFICIAL_EARLY_INTERVAL,
    CONF_OFFICIAL_INTERVAL,
    CONF_OVERLAP,
    CONF_PRICE_PATH,
    CONF_PRICE_RETRY,
    CONF_PRICE_TIME,
    CONF_PRIOR_METHOD,
    CONF_PRODUCTION_TYPE,
    CONF_REQUEST_DELAY,
    CONF_START_OFFSET,
    CONF_TIME_ZONE,
    CONF_TIMEOUT,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    PRICE_LOOKAHEAD_SECONDS,
    QUARTER_HOUR,
    PriorMethod,
    entry_data,
    entry_options,
)
from .estimate import Estimate, MonthData, estimate_month, recompute_month
from .schedule import Task
from .sources import (
    EControlConfig,
    EnergyChartsClient,
    EnergyChartsConfig,
    OfficialValue,
    SourceError,
    SourceRateLimitError,
    display_url,
    fetch_econtrol,
)

# A config entry whose `runtime_data` is our coordinator (typed, so editors and type checkers know).
type ReferenzmarktwertConfigEntry = ConfigEntry[ReferenzmarktwertCoordinator]

_LOGGER = logging.getLogger(__package__)

# settings that change which data is downloaded: if one of them changes, the stored data is discarded
_DATA_KEYS = (
    CONF_ENERGY_CHARTS_URL,
    CONF_GENERATION_PATH,
    CONF_PRICE_PATH,
    CONF_COUNTRY,
    CONF_BIDDING_ZONE,
    CONF_PRODUCTION_TYPE,
    CONF_ECONTROL_URL,
    CONF_ECONTROL_MONTH_COLUMN,
    CONF_ECONTROL_VALUE_COLUMN,
    CONF_ECONTROL_BASE_COLUMN,
    CONF_ECONTROL_PUBLISHED_COLUMN,
    CONF_TIME_ZONE,
)


def _headers(name: str, value: str) -> dict[str, str]:
    return {name: value} if name else {}


def energy_charts_config(data: Mapping[str, Any]) -> EnergyChartsConfig:
    """Energy-Charts settings from a config entry's data (missing keys: defaults)."""
    d = entry_data(data)
    return EnergyChartsConfig(
        base_url=d[CONF_ENERGY_CHARTS_URL],
        generation_path=d[CONF_GENERATION_PATH],
        price_path=d[CONF_PRICE_PATH],
        country=d[CONF_COUNTRY],
        bidding_zone=d[CONF_BIDDING_ZONE],
        production_type=d[CONF_PRODUCTION_TYPE],
        headers=_headers(d[CONF_HEADER_NAME], d[CONF_HEADER_VALUE]),
        timeout=float(d[CONF_TIMEOUT]),
    )


def econtrol_config(data: Mapping[str, Any]) -> EControlConfig:
    """E-Control settings from a config entry's data; an empty column means "not used"."""
    d = entry_data(data)
    return EControlConfig(
        url=d[CONF_ECONTROL_URL],
        month_column=d[CONF_ECONTROL_MONTH_COLUMN],
        value_column=d[CONF_ECONTROL_VALUE_COLUMN],
        base_column=d[CONF_ECONTROL_BASE_COLUMN] or None,
        published_column=d[CONF_ECONTROL_PUBLISHED_COLUMN] or None,
        headers=_headers(d[CONF_ECONTROL_HEADER_NAME], d[CONF_ECONTROL_HEADER_VALUE]),
        timeout=float(d[CONF_TIMEOUT]),
    )


def fingerprint(data: Mapping[str, Any]) -> str:
    """Identifies the data sources, so stored data of other sources is never used."""
    d = entry_data(data)
    text = json.dumps({key: str(d[key]).strip() for key in _DATA_KEYS}, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def schedule_settings(options: Mapping[str, Any]) -> schedule.Settings:
    o = entry_options(options)
    return schedule.Settings(
        interval=float(o[CONF_UPDATE_INTERVAL]) * 60,
        start_offset=float(o[CONF_START_OFFSET]) * 60,
        end_offset=float(o[CONF_END_OFFSET]) * 60,
        full_refresh_time=time.fromisoformat(o[CONF_FULL_REFRESH_TIME]),
        price_time=time.fromisoformat(o[CONF_PRICE_TIME]),
        price_retry=float(o[CONF_PRICE_RETRY]) * 60,
        official_interval=float(o[CONF_OFFICIAL_INTERVAL]) * 3600,
        official_early_interval=float(o[CONF_OFFICIAL_EARLY_INTERVAL]) * 3600,
        official_early_days=int(o[CONF_OFFICIAL_EARLY_DAYS]),
    )


@dataclass(frozen=True, slots=True)
class ReferenzmarktwertData:
    """What the entities show: the running month's estimate and the official table."""

    estimate: Estimate
    official: Mapping[Month, OfficialValue] = field(default_factory=dict)
    official_checked: datetime | None = None  # last successful check of the E-Control file
    deduction: float = 0.0

    @property
    def latest_official(self) -> OfficialValue | None:
        return self.official[max(self.official)] if self.official else None


class ReferenzmarktwertCoordinator(DataUpdateCoordinator[ReferenzmarktwertData]):
    """Downloads, keeps and evaluates the data of one config entry."""

    config_entry: ReferenzmarktwertConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ReferenzmarktwertConfigEntry) -> None:
        self._options = entry_options(entry.options)
        self._settings = schedule_settings(self._options)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=self._settings.interval),  # replaced after every run
            always_update=False,  # tell the sensors only when the data changed (it is a frozen dataclass)
        )
        session = async_get_clientsession(hass)
        self._session = session
        self._energy_charts = EnergyChartsClient(session, energy_charts_config(entry.data))
        self._econtrol = econtrol_config(entry.data)
        self._fingerprint = fingerprint(entry.data)
        self._tz_name: str = entry_data(entry.data)[CONF_TIME_ZONE]
        self._tz: tzinfo | None = None
        self._period = int(self._options[CONF_AGGREGATION_SECONDS])
        self._method = PriorMethod(self._options[CONF_PRIOR_METHOD])
        self._delay = float(self._options[CONF_REQUEST_DELAY])
        self._overlap = int(float(self._options[CONF_OVERLAP]) * 3600)
        self._store = CacheStore(hass, entry.entry_id)
        self.state = CacheState()
        self._official_failing = False
        self._energy_charts_failing = False
        self._closed = False  # unloaded: an update still running must not save any more

    async def _async_setup(self) -> None:
        """Runs once before the first update: load the time zone (reads a file) and the stored data."""
        self._tz = await dt_util.async_get_time_zone(self._tz_name)
        if self._tz is None:
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="invalid_time_zone",
                translation_placeholders={"time_zone": self._tz_name},
            )
        self.state = CacheState.from_dict(await self._store.async_load(), self._fingerprint)

    async def async_shutdown(self) -> None:
        """Called by Home Assistant on unload (also before the entry is removed)."""
        self._closed = True
        await super().async_shutdown()

    # --- the update ------------------------------------------------------------------------------

    async def _async_update_data(self) -> ReferenzmarktwertData:
        assert self._tz is not None  # set in _async_setup
        now = dt_util.utcnow().timestamp()
        month = calc.month_of(now, self._tz)
        tasks = schedule.plan(now, self._tz, self._settings, self._status(now, month)).tasks
        changed = bool(tasks)

        if Task.OFFICIAL in tasks:
            await self._refresh_official(now)
        error = await self._run_energy_charts(tasks, now, month)

        self._prune(month)
        if changed and not self._closed:
            await self._store.async_save(self.state.to_dict(self._fingerprint))
        next_run = schedule.plan(now, self._tz, self._settings, self._status(now, month)).next_run
        self.update_interval = timedelta(seconds=next_run - now)

        if self.state.full_refresh is None:
            # nothing downloaded yet: report it (setup is retried) instead of showing empty sensors
            raise error if error is not None else self._still_rate_limited(now)
        return self._evaluate(now, month)

    def _status(self, now: float, month: Month) -> schedule.Status:
        st = self.state
        prices_until = calc.covered_until(st.price.series())
        return schedule.Status(
            generation_attempt=st.generation_attempt,
            full_refresh=st.full_refresh,
            price_attempt=st.price_attempt,
            prices_until=prices_until if st.price.since is not None else None,
            reference_missing=st.reference_month != calc.shift_month(month, -12),
            official_attempt=st.official_attempt,
            # no table yet counts as failing, also after a restart: retry every interval, not daily
            official_failing=self._official_failing or not st.official,
            official_awaited=self._previous_pending(month),
            not_before=st.not_before,
            production=schedule.production_hours(st.generation.series(), self._tz, self._today(now)),
        )

    def _today(self, now: float) -> date:
        assert self._tz is not None
        return datetime.fromtimestamp(now, self._tz).date()

    def _evaluate(self, now: float, month: Month) -> ReferenzmarktwertData:
        assert self._tz is not None
        st = self.state
        current = MonthData(st.generation.series(), st.price.series())
        reference = MonthData(st.reference.series(), []) if st.reference_month == calc.shift_month(month, -12) else None
        previous = calc.shift_month(month, -1)
        recomputed = None
        if self._previous_pending(month) and self._covers(calc.month_bounds(previous, self._tz)[0]):
            recomputed = recompute_month(current, previous, self._tz, self._period)
        estimate = estimate_month(
            now,
            self._tz,
            current,
            reference,
            {m: v.value for m, v in st.official.items()},
            {m: v.base for m, v in st.official.items() if v.base is not None},
            recomputed,
            self._method,
            self._period,
        )
        return ReferenzmarktwertData(
            estimate=estimate,
            official=dict(st.official),
            official_checked=dt_util.utc_from_timestamp(st.official_checked) if st.official_checked else None,
            deduction=float(self._options[CONF_DEDUCTION]),
        )

    def _covers(self, start: int) -> bool:
        """Generation and prices are complete from `start` on."""
        gen, price = self.state.generation.since, self.state.price.since
        return gen is not None and price is not None and gen <= start and price <= start

    def _previous_pending(self, month: Month) -> bool:
        """E-Control has not published the previous month yet."""
        return calc.shift_month(month, -1) not in self.state.official

    def _keep_from(self, now: float, month: Month) -> int:
        """Start of the data needed: the running month, the previous one until it is official, and
        always yesterday (its generation sets the download window, also on the 1st of a month)."""
        assert self._tz is not None
        needed = calc.shift_month(month, -1) if self._previous_pending(month) else month
        yesterday = datetime.combine(self._today(now) - timedelta(days=1), time(), self._tz).timestamp()
        return min(calc.month_bounds(needed, self._tz)[0], int(yesterday))

    def _prune(self, month: Month) -> None:
        assert self._tz is not None
        keep = calc.month_bounds(calc.shift_month(month, -1), self._tz)[0]
        self.state.generation.prune(keep)
        self.state.price.prune(keep)

    # --- E-Control -------------------------------------------------------------------------------

    async def _refresh_official(self, now: float) -> None:
        """Check the E-Control file; it is only transferred if it changed. A failure keeps the table."""
        st = self.state
        st.official_attempt = now
        try:
            result = await fetch_econtrol(
                self._session, self._econtrol, st.official_last_modified if st.official else None
            )
        except SourceError as err:
            if not self._official_failing:  # log once when it starts failing ...
                _LOGGER.warning("E-Control file not available, keeping the last values: %s", err)
                self._official_failing = True
            return
        if self._official_failing:  # ... and once when it works again
            _LOGGER.info("E-Control file available again")
            self._official_failing = False
        if result.table is not None:
            st.official = result.table
            st.official_last_modified = result.last_modified
        st.official_checked = now

    # --- Energy-Charts ---------------------------------------------------------------------------

    async def _run_energy_charts(self, tasks: frozenset[Task], now: float, month: Month) -> UpdateFailed | None:
        """Run the due Energy-Charts downloads; stop at the first failure (the next ones would fail too)."""
        st = self.state
        downloads: list[Callable[[], Awaitable[None]]] = []
        if Task.FULL in tasks or Task.GENERATION in tasks:
            downloads.append(lambda: self._download_generation(now, month, full=Task.FULL in tasks))
        if Task.PRICE in tasks:
            downloads.append(lambda: self._download_prices(now, month))
        if Task.REFERENCE in tasks:
            downloads.append(lambda: self._download_reference(month))
        try:
            for number, download in enumerate(downloads):
                if number and self._delay > 0:  # a pause between requests, not after the last one
                    await asyncio.sleep(self._delay)
                await download()
        except SourceRateLimitError as err:
            wait = err.retry_after if err.retry_after is not None else self._settings.interval
            st.not_before = now + wait
            return self._failed(err, "rate_limited", retry_after=wait)
        except SourceError as err:
            return self._failed(err, "energy_charts_failed")
        if tasks & schedule.ENERGY_CHARTS_TASKS:
            if self._energy_charts_failing:
                _LOGGER.info("Energy-Charts available again")
                self._energy_charts_failing = False
            if not self._closed:  # an update finishing after removal must not bring the notice back
                self._report_deprecation()
        return None

    def _report_deprecation(self) -> None:
        """Show a repair notice while Energy-Charts marks the endpoints as deprecated (six months' notice)."""
        issue_id = deprecation_issue_id(self.config_entry.entry_id)
        if self._energy_charts.deprecated:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="energy_charts_deprecated",
                learn_more_url=display_url(self._energy_charts.config.base_url),
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _still_rate_limited(self, now: float) -> UpdateFailed:
        """Nothing downloaded yet because Energy-Charts asked us to wait (e.g. a retry after HTTP 429)."""
        wait = max((self.state.not_before or now) - now, 0.0)
        return UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="rate_limited",
            translation_placeholders={"error": f"waiting {wait:.0f} s"},
            retry_after=wait or None,
        )

    def _failed(self, err: SourceError, key: str, retry_after: float | None = None) -> UpdateFailed:
        if not self._energy_charts_failing:
            _LOGGER.warning("Energy-Charts not available, keeping the stored data: %s", err)
            self._energy_charts_failing = True
        return UpdateFailed(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={"error": str(err)},
            retry_after=retry_after,
        )

    async def _download_generation(self, now: float, month: Month, *, full: bool) -> None:
        """Full: from the start of the data needed. Otherwise only since the last stored point."""
        st = self.state
        st.generation_attempt = now
        keep_from = self._keep_from(now, month)
        last = st.generation.last()
        full = full or st.generation.since is None or st.generation.since > keep_from or last is None
        start = keep_from if full else max(keep_from, last - self._overlap)
        series = await self._energy_charts.generation(start, int(now))
        st.generation.merge(series, start, complete=full)
        if full:
            st.full_refresh = now

    async def _download_prices(self, now: float, month: Month) -> None:
        """Prices since the last stored one, up to what is published (tomorrow, after the auction)."""
        st = self.state
        st.price_attempt = now
        keep_from = self._keep_from(now, month)
        last = st.price.last()
        full = st.price.since is None or st.price.since > keep_from or last is None
        start = keep_from if full else last + QUARTER_HOUR
        series = await self._energy_charts.price(start, int(now) + PRICE_LOOKAHEAD_SECONDS)
        st.price.merge(series, start, complete=full)

    async def _download_reference(self, month: Month) -> None:
        """The same month last year, for the expected generation share (one download per month)."""
        assert self._tz is not None
        reference_month = calc.shift_month(month, -12)
        start, end = calc.month_bounds(reference_month, self._tz)
        series = await self._energy_charts.generation(start, end)
        self.state.reference_month = reference_month
        self.state.reference = SeriesCache(series, start)


def storage_key(entry_id: str) -> str:
    """The storage file's name in `<config>/.storage/`: one per entry, only ever this one."""
    return f"{DOMAIN}.{entry_id}"


def deprecation_issue_id(entry_id: str) -> str:
    return f"energy_charts_deprecated_{entry_id}"


class CacheStore(Store[dict[str, Any]]):
    """The entry's stored downloads: `<config>/.storage/referenzmarktwert.<entry_id>`.

    A file of another format version is not converted; the data is downloaded again. That is cheaper
    and safer than migration code for data that can be fetched anew:
    - older version: Home Assistant writes the (empty) migration result over it right at loading;
    - newer version (after a downgrade): loads as empty, replaced at the next save.
    A damaged file is renamed to `*.corrupt.<time>` by Home Assistant itself and loads as empty.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        super().__init__(hass, STORAGE_VERSION, storage_key(entry_id))

    async def async_load(self) -> dict[str, Any] | None:
        try:
            return await super().async_load()
        except UnsupportedStorageVersionError:
            _LOGGER.warning("Stored data is from a newer version of the integration, downloading it again")
            return None

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        return {}
