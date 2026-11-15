"""Estimate of the running month, put together from already fetched data.

Pure: no Home Assistant, no network, no clock (the caller passes `now` and the time zone). The
coordinator fetches and caches; this module decides what the numbers are. All prices returned are
EUR/kWh.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import tzinfo
from enum import StrEnum

from . import calc
from .calc import Month, Series
from .const import DEFAULT_AGGREGATION_SECONDS, DEFAULT_PRIOR_METHOD, KWH_PER_MWH, PriorMethod

QUARTER_HOUR = 900  # resolution of the Energy-Charts series


class PreviousSource(StrEnum):
    """Where the previous month's values come from."""

    OFFICIAL = "official"  # E-Control has published it
    RECOMPUTED = "recomputed"  # not yet published: recomputed from Energy-Charts
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class MonthData:
    """Energy-Charts data of one month: generation (MW) and prices (EUR/MWh), quarter-hourly."""

    generation: Series
    price: Series


@dataclass(frozen=True, slots=True)
class Estimate:
    """The running month's estimate and how it was obtained (for sensor attributes)."""

    month: Month
    value: float | None  # whole-month estimate
    month_so_far: float | None  # generation-weighted value of the complete hours so far
    known_share: float | None  # expected share of the month's generation already known (0..1)
    prior: float | None  # estimate for the rest of the month
    generation_until: int | None  # end of the last complete hour used (Unix seconds)
    prices_until: int | None  # end of the last known day-ahead price
    running_base: float | None  # base price of the month's known prices
    previous_month: float | None
    previous_source: PreviousSource
    method: PriorMethod


def recompute_month(
    data: MonthData, month: Month, tz: tzinfo, period: int = DEFAULT_AGGREGATION_SECONDS
) -> tuple[float | None, float | None]:
    """A complete month recomputed from Energy-Charts: (value, base price) in EUR/kWh."""
    start, end = calc.month_bounds(month, tz)
    value = calc.market_value(data.generation, data.price, start, end, period)
    base = calc.base_price(data.price, start, end, period)
    return (
        value.eur_per_kwh if value else None,
        base / KWH_PER_MWH if base is not None else None,
    )


def estimate_month(
    now: float,
    tz: tzinfo,
    current: MonthData,
    reference: MonthData | None,
    official_value: Mapping[Month, float],
    official_base: Mapping[Month, float],
    previous_recomputed: tuple[float | None, float | None] | None = None,
    method: PriorMethod = DEFAULT_PRIOR_METHOD,
    period: int = DEFAULT_AGGREGATION_SECONDS,
) -> Estimate:
    """Estimate the month containing `now`.

    current:   Energy-Charts data from the month's start (prices may reach into the next days)
    reference: generation of the same month one year earlier (for the expected generation share)
    official_value / official_base: E-Control's published months, EUR/kWh
    previous_recomputed: (value, base) of the previous month from Energy-Charts, used until
        E-Control has published it
    """
    month = calc.month_of(now, tz)
    start, end = calc.month_bounds(month, tz)
    previous = calc.shift_month(month, -1)

    # Only complete hours count: a partial latest hour would be weighted with an incomplete mean.
    gen_end = calc.covered_until(current.generation, calc.resolution(current.generation, QUARTER_HOUR))
    generation_until = min(calc.floor_to(gen_end, period), end) if gen_end is not None else None
    so_far = (
        calc.market_value(current.generation, current.price, start, generation_until, period)
        if generation_until is not None and generation_until > start
        else None
    )

    known_share = None
    if reference is not None and generation_until is not None:
        last_year = calc.shift_month(month, -12)
        ref_start, ref_end = calc.month_bounds(last_year, tz)
        ref = calc.aggregate(reference.generation, ref_start, ref_end, period)
        known_share = calc.elapsed_share(ref, ref_start, generation_until - start)

    # What is known about the previous month: official if published, else recomputed.
    pv, base = dict(official_value), dict(official_base)
    if previous in pv:
        previous_source = PreviousSource.OFFICIAL
    elif previous_recomputed is not None and previous_recomputed[0] is not None:
        previous_source = PreviousSource.RECOMPUTED
        pv[previous] = previous_recomputed[0]
        if previous_recomputed[1] is not None:
            base[previous] = previous_recomputed[1]
    else:
        previous_source = PreviousSource.MISSING

    price_end = calc.covered_until(current.price, calc.resolution(current.price, QUARTER_HOUR))
    prices_until = min(price_end, end) if price_end is not None else None
    running_base = None
    weight = 0.0
    if prices_until is not None and prices_until > start:
        level = calc.base_price(current.price, start, prices_until, period)
        running_base = level / KWH_PER_MWH if level is not None else None
        weight = calc.time_share(start, end, prices_until)

    prior = calc.prior(month, pv, base, method, running_base, weight)
    so_far_value = so_far.eur_per_kwh if so_far else None
    return Estimate(
        month=month,
        value=calc.blend(so_far_value, prior, known_share),
        month_so_far=so_far_value,
        known_share=known_share,
        prior=prior,
        generation_until=generation_until,
        prices_until=prices_until,
        running_base=running_base,
        previous_month=pv.get(previous),
        previous_source=previous_source,
        method=method,
    )
