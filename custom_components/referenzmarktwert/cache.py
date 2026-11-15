"""Downloaded data kept between updates and Home Assistant restarts.

Pure: no Home Assistant, no network, no clock. The coordinator merges every download in here and
saves `to_dict()` with Home Assistant's storage helper; `from_dict()` restores it after a restart,
so a restart costs no downloads.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .calc import Month, Series
from .sources import OfficialValue

STORAGE_VERSION = 1


class SeriesCache:
    """A quarter-hourly time series assembled from several downloads.

    `since` marks the start of the data known to be complete: everything from `since` on has been
    downloaded (a point may still be None if the source had no value). None = nothing complete yet.
    """

    def __init__(self, points: Iterable[tuple[int, float | None]] = (), since: int | None = None) -> None:
        self._points: dict[int, float | None] = dict(points)
        self.since = since

    def merge(self, series: Series, start: int, *, complete: bool) -> None:
        """Put a download in; its points replace the stored ones in the range it covers.

        `complete`: the download covers everything from `start` on (a full download), so `since`
        can move back to `start`. An incremental download only adds and replaces points.
        """
        new = dict(series)
        if new:
            first, last = min(new), max(new)
            self._points = {ts: v for ts, v in self._points.items() if not first <= ts <= last}
            self._points.update(new)
        if complete:
            self.since = start if self.since is None else min(self.since, start)

    def series(self) -> list[tuple[int, float | None]]:
        return sorted(self._points.items())

    def last(self) -> int | None:
        """Timestamp of the latest point with a value."""
        stamps = [ts for ts, v in self._points.items() if v is not None]
        return max(stamps) if stamps else None

    def prune(self, before: int) -> None:
        """Forget everything before `before` (older months are not needed any more)."""
        self._points = {ts: v for ts, v in self._points.items() if ts >= before}
        if self.since is not None and self.since < before:
            self.since = before

    def __len__(self) -> int:
        return len(self._points)

    def to_dict(self) -> dict[str, Any]:
        return {"since": self.since, "points": [[ts, v] for ts, v in self.series()]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SeriesCache:
        return cls(((int(ts), v) for ts, v in data.get("points", [])), data.get("since"))


@dataclass
class CacheState:
    """Everything the coordinator downloaded, and when it last tried what (Unix seconds)."""

    generation: SeriesCache = field(default_factory=SeriesCache)
    price: SeriesCache = field(default_factory=SeriesCache)
    reference_month: Month | None = None  # the same month last year, for the expected generation share
    reference: list[tuple[int, float | None]] = field(default_factory=list)
    official: dict[Month, OfficialValue] = field(default_factory=dict)
    official_last_modified: str | None = None  # Last-Modified of the file the table came from
    official_checked: float | None = None  # last successful check (downloaded or "not modified")
    # when what was last done
    generation_attempt: float | None = None
    full_refresh: float | None = None  # last successful full download of the running month
    price_attempt: float | None = None
    official_attempt: float | None = None
    not_before: float | None = None  # Energy-Charts asked us to wait (HTTP 429) until then

    def to_dict(self, fingerprint: str) -> dict[str, Any]:
        return {
            "fingerprint": fingerprint,
            "generation": self.generation.to_dict(),
            "price": self.price.to_dict(),
            "reference_month": list(self.reference_month) if self.reference_month else None,
            "reference": [[ts, v] for ts, v in self.reference],
            "official": [
                [v.month[0], v.month[1], v.value, v.base, v.published.isoformat() if v.published else None]
                for v in self.official.values()
            ],
            "official_last_modified": self.official_last_modified,
            "official_checked": self.official_checked,
            "generation_attempt": self.generation_attempt,
            "full_refresh": self.full_refresh,
            "price_attempt": self.price_attempt,
            "official_attempt": self.official_attempt,
            "not_before": self.not_before,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, fingerprint: str) -> CacheState:
        """Restore a saved state; start empty if there is none or it belongs to other data sources."""
        if not data or data.get("fingerprint") != fingerprint:
            return cls()
        official = {}
        for year, month, value, base, published in data.get("official", []):
            official[(year, month)] = OfficialValue(
                (year, month), value, base, date.fromisoformat(published) if published else None
            )
        reference_month = data.get("reference_month")
        return cls(
            generation=SeriesCache.from_dict(data.get("generation", {})),
            price=SeriesCache.from_dict(data.get("price", {})),
            reference_month=(reference_month[0], reference_month[1]) if reference_month else None,
            reference=[(int(ts), v) for ts, v in data.get("reference", [])],
            official=official,
            # a Last-Modified date without the table it belongs to must not be used for a conditional request
            official_last_modified=data.get("official_last_modified") if official else None,
            official_checked=data.get("official_checked"),
            generation_attempt=data.get("generation_attempt"),
            full_refresh=data.get("full_refresh"),
            price_attempt=data.get("price_attempt"),
            official_attempt=data.get("official_attempt"),
            not_before=data.get("not_before"),
        )
