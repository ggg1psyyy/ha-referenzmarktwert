"""Stored downloads (cache.py): merging, pruning, saving and restoring."""

from datetime import date

import pytest

from custom_components.referenzmarktwert.cache import CacheState, SeriesCache
from custom_components.referenzmarktwert.sources import OfficialValue


def test_merge_replaces_the_covered_range_only() -> None:
    cache = SeriesCache()
    cache.merge([(0, 1.0), (900, 2.0), (1800, 3.0)], 0, complete=True)
    cache.merge([(900, 20.0), (2700, 4.0)], 900, complete=False)  # overlap: 900 corrected
    assert cache.series() == [(0, 1.0), (900, 20.0), (2700, 4.0)]  # 1800 is gone: inside the new range
    assert cache.since == 0
    assert cache.last() == 2700


def test_empty_download_changes_nothing_but_completes() -> None:
    """Energy-Charts answers 'no content' at the very start of a month: that is complete, too."""
    cache = SeriesCache()
    cache.merge([], 100, complete=True)
    assert len(cache) == 0
    assert cache.since == 100
    assert cache.last() is None


def test_incremental_download_does_not_claim_completeness() -> None:
    cache = SeriesCache()
    cache.merge([(900, 1.0)], 900, complete=False)
    assert cache.since is None


def test_last_ignores_missing_values() -> None:
    cache = SeriesCache([(0, 1.0), (900, None)], 0)
    assert cache.last() == 0


def test_prune() -> None:
    cache = SeriesCache([(0, 1.0), (900, 2.0)], 0)
    cache.prune(900)
    assert cache.series() == [(900, 2.0)]
    assert cache.since == 900


def test_state_round_trip() -> None:
    state = CacheState(
        generation=SeriesCache([(0, 1.0), (900, None)], 0),
        price=SeriesCache([(0, 50.0)], 0),
        reference_month=(2025, 11),
        reference=SeriesCache([(10, 2.0)], 0),
        official={(2026, 10): OfficialValue((2026, 10), 0.1771, 0.1034, date(2026, 11, 4))},
        official_last_modified="Thu, 05 Nov 2026 14:54:13 GMT",
        official_checked=5.0,
        generation_attempt=1.0,
        full_refresh=2.0,
        price_attempt=3.0,
        official_attempt=4.0,
        not_before=6.0,
    )
    restored = CacheState.from_dict(state.to_dict("abc"), "abc")
    assert restored.generation.series() == state.generation.series()
    assert restored.generation.since == 0
    assert restored.price.series() == [(0, 50.0)]
    assert restored.reference_month == (2025, 11)
    assert restored.reference.series() == [(10, 2.0)]
    assert restored.official == state.official
    assert restored.official_last_modified == state.official_last_modified
    assert (restored.generation_attempt, restored.full_refresh, restored.price_attempt) == (1.0, 2.0, 3.0)
    assert (restored.official_attempt, restored.official_checked, restored.not_before) == (4.0, 5.0, 6.0)


def test_other_data_sources_start_empty() -> None:
    """After a Reconfigure (other country, zone, ...) the stored data must not be used."""
    saved = CacheState(generation=SeriesCache([(0, 1.0)], 0)).to_dict("old")
    assert len(CacheState.from_dict(saved, "new").generation) == 0
    assert len(CacheState.from_dict(None, "new").generation) == 0


def test_last_modified_needs_its_table() -> None:
    """A Last-Modified date without the table would make E-Control answer 304 and leave us empty."""
    saved = CacheState(official_last_modified="Thu, 05 Nov 2026 14:54:13 GMT").to_dict("x")
    assert CacheState.from_dict(saved, "x").official_last_modified is None


@pytest.mark.parametrize(
    "damage",
    [
        {"generation": {"points": [["x", 1.0]]}},  # not a timestamp
        {"generation": {"points": [[1]]}},  # a point without value
        {"official": [[2026, 10]]},  # a month without values
        {"official": [[2026, 10, 0.1, None, "not a date"]]},
        {"reference_month": 2025},
        {"price": "nonsense"},
    ],
)
def test_damaged_data_starts_empty(damage: dict) -> None:
    """Unreadable stored data must not stop the integration: it is discarded and downloaded again."""
    saved = {**CacheState(generation=SeriesCache([(0, 1.0)], 0)).to_dict("x"), **damage}
    assert len(CacheState.from_dict(saved, "x").generation) == 0
