"""Backtest the running-month estimate against E-Control's official values.

For every month and several days d of the month, the estimate is computed with only the data that
was available at midnight starting day d+1 (generation lags 2 h, day-ahead prices are known until the
end of the next day, last month's official value only after its publication date) and compared with
the official value. Uses the integration's own calc/sources functions.

    python tools/backtest_estimate.py --cache /some/dir          # downloads ~59 months once, politely

Energy-Charts data: Fraunhofer ISE / Bundesnetzagentur SMARD.de, CC BY 4.0.
"""

import argparse
import asyncio
import json
import statistics as st
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from custom_components.referenzmarktwert import calc, sources

VIENNA = ZoneInfo("Europe/Vienna")
LAG = 2 * 3600
DAYS = (1, 3, 5, 7, 10, 15, 20, 25)
FIRST, LAST = (2022, 1), (2026, 10)


def bounds(month: calc.Month) -> tuple[int, int]:
    nxt = calc.shift_month(month, 1)
    start = datetime(month[0], month[1], 1, tzinfo=VIENNA)
    return int(start.timestamp()), int(datetime(nxt[0], nxt[1], 1, tzinfo=VIENNA).timestamp())


async def download(cache: Path, months: list[calc.Month]) -> None:
    async with aiohttp.ClientSession() as session:
        client = sources.EnergyChartsClient(session)
        for month in months:
            file = cache / f"{month[0]}-{month[1]:02d}.json"
            if file.exists():
                continue
            start, end = bounds(month)
            result = {}
            for name, fetch in (("gen", client.generation), ("price", client.price)):
                for attempt in range(6):
                    try:
                        result[name] = await fetch(start, end)
                        break
                    except sources.SourceConnectionError as err:  # includes 429
                        wait = getattr(err, "retry_after", None) or 10 * 2**attempt
                        print(f"{month}: {err}, waiting {wait} s", file=sys.stderr)
                        await asyncio.sleep(wait)
                else:
                    raise SystemExit(f"{month}: giving up")
                await asyncio.sleep(3)
            file.write_text(json.dumps(result))
            print(f"downloaded {month}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", type=Path, required=True, help="directory for downloaded Energy-Charts data")
    parser.add_argument(
        "--xlsx", type=Path, default=Path(__file__).parents[1] / "tests/fixtures/Referenzmarktwert_Entwicklung.xlsx"
    )
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)

    months, m = [], FIRST
    while m <= LAST:
        months.append(m)
        m = calc.shift_month(m, 1)
    asyncio.run(download(args.cache, months))

    official = sources.parse_econtrol_xlsx(args.xlsx.read_bytes())
    pv = {k: v.value * 100 for k, v in official.items()}  # ct/kWh
    base = {k: v.base * 100 for k, v in official.items() if v.base is not None}
    data = {}
    for month in months:
        raw = json.loads((args.cache / f"{month[0]}-{month[1]:02d}.json").read_text())
        data[month] = ([tuple(x) for x in raw["gen"]], [tuple(x) for x in raw["price"]])

    def recomputed(month: calc.Month) -> tuple[float | None, float | None]:
        start, end = bounds(month)
        value = calc.market_value(*data[month], start, end)
        level = calc.base_price(data[month][1], start, end)
        return (value.eur_per_mwh / 10 if value else None, level / 10 if level is not None else None)

    methods = {
        "month so far only": None,
        "previous month": calc.PriorMethod.PREVIOUS_MONTH,
        "seasonal difference": calc.PriorMethod.SEASONAL_DIFFERENCE,
        "base x capture rate, previous base only": "prev-only",
        "base x capture rate (default)": calc.PriorMethod.BASE_X_CAPTURE_RATE,
    }
    errors: dict[tuple[str, int], list[float]] = {(k, d): [] for k in methods for d in DAYS}
    evaluated = [m for m in months if calc.shift_month(m, -13) >= FIRST]
    for month in evaluated:
        start, end = bounds(month)
        prev, last_year = calc.shift_month(month, -1), calc.shift_month(month, -12)
        ly_start, ly_end = bounds(last_year)
        reference = calc.aggregate(data[last_year][0], ly_start, ly_end)
        for day in DAYS:
            midnight = int((datetime.fromtimestamp(start, VIENNA) + timedelta(days=day)).timestamp())
            known_gen, known_price = midnight - LAG, min(end, midnight + 86400)
            so_far = calc.market_value(*data[month], start, known_gen)
            so_far_ct = so_far.eur_per_mwh / 10 if so_far else None
            share = calc.elapsed_share(reference, ly_start, known_gen - start)
            # what was known about the previous month on that day
            pv_known, base_known = dict(pv), dict(base)
            published = official[prev].published
            if published is None or published > datetime.fromtimestamp(midnight, VIENNA).date():
                pv_known[prev], base_known[prev] = recomputed(prev)
            running = calc.base_price(data[month][1], start, known_price)
            running_ct = running / 10 if running is not None else None
            weight = calc.time_share(start, end, known_price)
            for name, method in methods.items():
                if method is None:
                    estimate = so_far_ct
                elif method == "prev-only":
                    estimate = calc.blend(so_far_ct, calc.prior(month, pv_known, base_known), share)
                else:
                    p = calc.prior(month, pv_known, base_known, method, running_ct, weight)
                    estimate = calc.blend(so_far_ct, p, share)
                errors[(name, day)].append(estimate - pv[month])

    floor = [recomputed(m)[0] - pv[m] for m in evaluated]
    print(f"Months {evaluated[0]}..{evaluated[-1]} (n={len(evaluated)}), errors in ct/kWh against the official value")
    print(
        f"Data floor (whole month recomputed): RMSE {st.fmean(x * x for x in floor) ** 0.5:.2f}, "
        f"max {max(abs(x) for x in floor):.2f}\n"
    )
    print(f"{'RMSE on day':40s}" + "".join(f"{d:>7d}" for d in DAYS))
    for name in methods:
        row = [st.fmean(x * x for x in errors[(name, d)]) ** 0.5 for d in DAYS]
        print(f"{name:40s}" + "".join(f"{v:7.2f}" for v in row))


if __name__ == "__main__":
    main()
