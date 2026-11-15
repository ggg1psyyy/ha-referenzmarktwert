"""Pure calculation of the reference market value (Referenzmarktwert, § 13 EAG).

No Home Assistant, no network, no clock: every input is passed in, so everything here is unit-testable.

Units: prices in EUR/MWh as delivered by the exchange (`eur_per_kwh` converts for Home Assistant),
generation in MW. Never cents: Home Assistant books prices in the main currency unit.
Timestamps are Unix seconds (UTC), the same as the Energy-Charts API delivers.

    RMW = sum_h(price_h * gen_h) / sum_h(gen_h)

where each h is one aggregation period (an hour by default): the price is the mean of the period's
quarter-hour prices, the generation the mean power (MW) over the period = its energy in MWh.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from statistics import fmean

from .const import DEFAULT_AGGREGATION_SECONDS, DEFAULT_PRIOR_METHOD, KWH_PER_MWH, PriorMethod

# A time series as (unix_seconds, value) pairs. value may be None (missing data point).
type Series = Iterable[tuple[int, float | None]]

# A calendar month as (year, month), e.g. (2026, 10). Tuples compare and hash naturally.
type Month = tuple[int, int]


def aggregate(
    series: Series,
    start: int,
    end: int,
    period: int = DEFAULT_AGGREGATION_SECONDS,
) -> dict[int, float]:
    """Average the points of `series` into periods of `period` seconds.

    Only points with start <= ts < end are used (half-open window; Energy-Charts includes `end`).
    Returns {period_start_ts: mean_value}; missing (None) points are skipped, and a period with no
    valid point is absent from the result.
    """
    buckets: defaultdict[int, list[float]] = defaultdict(list)
    for ts, value in series:
        if value is None or not start <= ts < end:
            continue
        buckets[ts - ts % period].append(value)
    return {bucket: fmean(values) for bucket, values in buckets.items()}


@dataclass(frozen=True, slots=True)
class WeightedValue:
    """Result of a generation-weighted price average."""

    eur_per_mwh: float  # the weighted mean price
    energy_mwh: float  # total generation of the periods used (the weight sum)
    periods: int  # number of periods that had both a price and generation
    last_period: int | None  # start ts of the latest period used (= "data until")

    @property
    def eur_per_kwh(self) -> float:
        """The unit Home Assistant's Energy Dashboard expects for prices."""
        return self.eur_per_mwh / KWH_PER_MWH


def weighted_value(
    generation: Mapping[int, float],
    price: Mapping[int, float],
    period: int = DEFAULT_AGGREGATION_SECONDS,
) -> WeightedValue | None:
    """Generation-weighted mean price over all periods present in both inputs.

    `generation` and `price` are aggregated series (see `aggregate`). Negative prices count normally.
    `period` converts mean power (MW) into energy (MWh) so `energy_mwh` is meaningful.
    Returns None if there is no generation to weight with (e.g. only night hours so far).
    """
    hours = period / 3600
    numerator = denominator = 0.0
    used = 0
    last: int | None = None
    for ts, gen_mw in generation.items():
        price_eur = price.get(ts)
        if price_eur is None:
            continue
        energy = gen_mw * hours
        numerator += price_eur * energy
        denominator += energy
        used += 1
        last = ts if last is None else max(last, ts)
    if denominator <= 0:
        return None
    return WeightedValue(numerator / denominator, denominator, used, last)


def market_value(
    generation: Series,
    price: Series,
    start: int,
    end: int,
    period: int = DEFAULT_AGGREGATION_SECONDS,
) -> WeightedValue | None:
    """Convenience: aggregate both raw series into periods, then weight. See module docstring."""
    return weighted_value(
        aggregate(generation, start, end, period),
        aggregate(price, start, end, period),
        period,
    )


def elapsed_share(reference: Mapping[int, float], reference_start: int, elapsed: int) -> float | None:
    """Share (0..1) of a reference month's energy produced in its first `elapsed` seconds.

    `reference` is an aggregated generation series of a comparable month (e.g. the same month last
    year) starting at `reference_start`. Offsets are compared, not dates, so the reference month may
    be any month. Pass `elapsed` from the latest data point, not the wall clock (data lags ~2 h).
    Returns None if the reference has no energy (unknown share).
    """
    total = sum(reference.values())
    if total <= 0:
        return None
    done = sum(v for ts, v in reference.items() if ts - reference_start < elapsed)
    return min(1.0, max(0.0, done / total))


def shift_month(month: Month, delta: int) -> Month:
    """Month `delta` months after `month` (negative = before)."""
    index = month[0] * 12 + month[1] - 1 + delta
    return index // 12, index % 12 + 1


def prior(
    month: Month,
    pv: Mapping[Month, float],
    base: Mapping[Month, float],
    method: PriorMethod = DEFAULT_PRIOR_METHOD,
) -> float | None:
    """Estimate a whole month's value from earlier months only (the blend's "prior").

    `pv` maps months to the technology's reference market value, `base` to the day-ahead base
    price, both in the same unit. If the chosen method lacks data, it falls back to the previous
    month's value; None if even that is missing.
    """
    prev, last_year, last_year_prev = (shift_month(month, d) for d in (-1, -12, -13))
    fallback = pv.get(prev)
    match method:
        case PriorMethod.BASE_X_CAPTURE_RATE:
            if prev in base and last_year in pv and base.get(last_year):
                return base[prev] * pv[last_year] / base[last_year]
        case PriorMethod.SEASONAL_DIFFERENCE:
            if fallback is not None and last_year in pv and last_year_prev in pv:
                return fallback + pv[last_year] - pv[last_year_prev]
        case PriorMethod.PREVIOUS_MONTH:
            pass
    return fallback


def blend(month_so_far: float | None, previous: float | None, known_share: float | None) -> float | None:
    """Whole-month estimate: month so far, weighted by the expected PV share already known,
    the rest filled with the previous month's value. Any input may be missing; with an unknown
    share the month so far is used as is (never silently discard real data)."""
    if month_so_far is None:
        return previous
    if previous is None or known_share is None:
        return month_so_far
    share = min(1.0, max(0.0, known_share))
    return share * month_so_far + (1 - share) * previous


def payout(value: float, deduction: float) -> float:
    """Feed-in price = market value - deduction (same unit for both, normally EUR/kWh)."""
    return value - deduction
