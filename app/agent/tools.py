"""Tools the agent can call. Plain Python over the deterministic engine: the LLM picks
the tool and the arguments, the numbers always come from here."""
import statistics

import anthropic
import pandas as pd
from pydantic import BaseModel, ValidationError

from app.agent.schemas import (CompareHubsInput, ExplainHubInput, HubBreakdownInput, RankHubsInput,
                               WeatherStatInput)
from app.config import load_scoring
from app.scoring import service
from app.scoring.disruption import rank
from app.scoring.periods import SEASON_NOTE, breakdown, describe, months_of, period_view
from app.settings import settings

# Hubs whose high-wind days are likely overstated (grid cell partly over water).
# See docs/DATA_NOTES.md, "Known limitation: coastal wind".
COASTAL_WIND_HUBS = {"virginia_beach", "oakland", "boston"}

METHOD = {
    "kpi": "expected disruption days per year (1.0 = one full operating day lost; partial days count as fractions)",
    "weather_period": f"{settings.history_start}..{settings.history_end} (Open-Meteo daily reanalysis)",
    # Stated explicitly so the model never has to derive it from the dates (eval run 1 finding).
    "weather_years": int(settings.history_end[:4]) - int(settings.history_start[:4]) + 1,
    "declarations_period": "2006..2026, FEMA major-disaster (DR) declarations, hurricanes and severe storms",
    "rules": "config/disruption.yaml, expert judgment, not calibrated on company downtime data",
    # "last year" in a question means this year (the last full year in the data).
    "latest_full_year": int(settings.history_end[:4]),
    "seasons": SEASON_NOTE,
}


def _method() -> dict:
    """METHOD plus the scoring run the numbers come from (traceable to rules and data)."""
    return {**METHOD, "scoring_run": service.run_id()}


# What each hazard's number is built from, so the model can state the limits of the comparison.
HAZARD_NOTES = {
    "tropical": "FEMA major-disaster declarations for the hub's county over 20 years (7 days per "
                "hurricane / tropical storm); flooding during a declared hurricane counts here, not under flood",
    "severe_storm": "FEMA major-disaster declarations for severe storms / tornadoes over 20 years (1.5 days each)",
    "flood": "daily rainfall thresholds over the 5-year weather window only; rare river or storm-surge "
             "floods outside that window are not captured (FEMA National Risk Index not included)",
    "winter": "daily snowfall, ice-proxy and extreme-cold thresholds over the 5-year weather window",
    "high_wind": "daily maximum gust thresholds over the 5-year weather window",
    "heat": "daily maximum temperature threshold (43 C) over the 5-year weather window",
}
SINGLE_HAZARDS = list(HAZARD_NOTES)

def _ranks(hazard: str, months: frozenset[int] | None = None) -> dict[str, int]:
    """hub_id -> rank among all hubs with complete data, for one hazard or the total, in a period."""
    out = rank(service.results(), hazard, None, 50, months)
    return {row["hub_id"]: row["rank"] for row in out["ranking"]}


def _leader(values: dict[str, float]) -> dict:
    """Highest, lowest and the difference, pre-computed: the model must not subtract by itself."""
    top = sorted(values.items(), key=lambda kv: -kv[1])
    return {"highest": top[0][0], "lowest": top[-1][0],
            "difference_highest_minus_lowest_days": round(top[0][1] - top[-1][1], 2),
            "tied": top[0][1] == top[-1][1]}


def _period_info(months) -> dict:
    return {"period": describe(months), **({"seasons_definition": SEASON_NOTE} if months else {})}


def _caveats(r) -> list[str]:
    out = []
    if r.hub_id in COASTAL_WIND_HUBS and r.hazards["high_wind"].days_per_year > 0:
        out.append("high-wind days likely overstated (coastal grid cell partly over water)")
    if not r.weather_complete:
        out.append(f"incomplete weather data ({r.weather_coverage:.0%} of days): not ranked against other hubs")
    return out


def _hazard_detail(r, hazard: str, ranks: dict[str, int], n_drivers: int) -> dict:
    h = r.hazards[hazard]
    return {
        "label": h.label,
        "days_per_year": h.days_per_year,
        "rank_among_hubs": ranks.get(r.hub_id),
        "share_of_hub_total_pct": round(100 * h.days_per_year / r.total_days_per_year) if r.total_days_per_year else 0,
        "from_weather": h.from_weather,
        "from_fema_declarations": h.from_declarations,
        "events_per_year": h.events_per_year,
        "top_drivers": [{"label": d.label, "occurrences_in_data": d.occurrences, "days_per_year": d.days_per_year}
                        for d in h.drivers[:n_drivers]],
        "fema_disasters_counted": [{"title": d.title, "declared": d.declaration_date, "days_counted": d.days}
                                   for d in r.declarations_counted if d.hazard == hazard],
        "built_from": HAZARD_NOTES[hazard],
    }


def explain_hub(args: ExplainHubInput) -> dict:
    r = service.by_id(args.hub_id)
    totals = [x.total_days_per_year for x in service.results() if x.weather_complete]
    median = round(statistics.median(totals), 2)
    order = sorted((h for h in SINGLE_HAZARDS if r.hazards[h].days_per_year > 0),
                   key=lambda h: -r.hazards[h].days_per_year)
    overall = _ranks("total").get(r.hub_id)
    return {
        "hub": {k: getattr(r, k) for k in ("hub_id", "city", "state", "region")},
        "total_days_per_year": r.total_days_per_year,
        # Context pre-computed so the model can say whether "high" is true without doing arithmetic.
        "context": {
            "overall_rank": overall,
            "hubs_ranked": len(totals),
            "position": None if overall is None else
                        "top 10" if overall <= 10 else "upper half" if overall <= len(totals) / 2 else "lower half",
            "median_hub_days_per_year": median,
            "difference_vs_median_days": round(r.total_days_per_year - median, 2),
            "highest_hub_days_per_year": max(totals),
        },
        "hazards_by_contribution": {h: _hazard_detail(r, h, _ranks(h), 3) for h in order},
        "hazards_with_zero_days": [h for h in SINGLE_HAZARDS if r.hazards[h].days_per_year == 0],
        "weather_days_dropped_as_covered_by_fema_disasters": r.weather_days_deduplicated,
        "caveats": _caveats(r),
        "method": _method(),
    }


def compare_hubs(args: CompareHubsInput) -> dict:
    months = months_of(args.season, args.months)
    ids = list(dict.fromkeys(args.hub_ids))  # keep order, drop repeats
    hazards = list(dict.fromkeys(args.hazards)) if args.hazards else SINGLE_HAZARDS
    hubs = [service.by_id(i) for i in ids]
    ranks = {h: _ranks(h, months) for h in ["total", *hazards]}
    views = {r.hub_id: period_view(r, months) for r in hubs}

    def days(r, h):
        return views[r.hub_id].get(h, {}).get("days_per_year", 0.0)

    def detail(r, h):
        if months is None:
            return _hazard_detail(r, h, ranks[h], 2)
        v = views[r.hub_id].get(h, {"days_per_year": 0.0, "events_in_data": 0, "from_weather": 0.0,
                                     "from_fema_declarations": 0.0, "top_drivers": []})
        return {"label": r.hazards[h].label, **v, "rank_among_hubs": ranks[h].get(r.hub_id),
                "fema_disasters_counted": [{"title": d.title, "incident_start": d.incident_begin_date,
                                            "days_counted": d.days} for d in r.declarations_counted
                                           if d.hazard == h and int((d.incident_begin_date or d.declaration_date)[5:7]) in months],
                "built_from": HAZARD_NOTES[h]}

    total = {r.hub_id: sum(days(r, h) for h in SINGLE_HAZARDS) for r in hubs}
    rows = [{
        "hub": {k: getattr(r, k) for k in ("hub_id", "city", "state", "region")},
        "total_days_per_year": round(total[r.hub_id], 3),
        "overall_rank": ranks["total"].get(r.hub_id),
        "hazards": {h: detail(r, h) for h in hazards},
        "caveats": _caveats(r),
    } for r in hubs]
    comparison = {h: _leader({r.city: days(r, h) for r in hubs}) for h in hazards}
    if args.hazards:
        comparison["selected_hazards_combined"] = _leader(
            {r.city: round(sum(days(r, h) for h in hazards), 3) for r in hubs})
        for row, r in zip(rows, hubs):
            row["selected_hazards_days_per_year"] = round(sum(days(r, h) for h in hazards), 3)
    comparison["total"] = _leader({r.city: round(total[r.hub_id], 3) for r in hubs})
    return {**_period_info(months), "hubs": rows, "comparison": comparison, "hubs_ranked": len(ranks["total"]),
            "method": _method()}


def rank_hubs(args: RankHubsInput) -> dict:
    months = months_of(args.season, args.months)
    out = rank(service.results(), args.hazard, args.region, args.top_n, months, args.order)
    labels = load_scoring().hazards
    rows = []
    for row in out["ranking"]:
        r = service.by_id(row["hub_id"])
        view = period_view(r, months)
        by_hazard = dict(sorted(((h, v["days_per_year"]) for h, v in view.items() if v["days_per_year"] > 0),
                                key=lambda kv: -kv[1]))
        main = next(iter(by_hazard), None) if args.hazard == "total" else args.hazard
        rows.append({
            **{k: row[k] for k in ("rank", "hub_id", "city", "state", "region", "days_per_year")},
            "main_hazard": labels[main] if main else None,
            "by_hazard": by_hazard,
            "top_drivers": view[main]["top_drivers"] if main in view else [],
            **({"caveat": "high-wind days likely overstated (coastal grid cell partly over water)"}
               if r.hub_id in COASTAL_WIND_HUBS and r.hazards["high_wind"].days_per_year > 0 else {}),
        })
    counts: dict[str, int] = {}
    for row in rows:
        if row["main_hazard"]:
            counts[row["main_hazard"]] = counts.get(row["main_hazard"], 0) + 1
    return {
        "hazard": args.hazard if args.hazard == "total" else f"{args.hazard} ({labels[args.hazard]})",
        "region": args.region,
        **_period_info(months),
        "order": "most exposed first" if args.order == "most" else "least exposed (safest) first",
        "ranking": rows,
        # Pre-computed so the model never has to count or subtract by itself.
        "summary": {
            "hubs_returned": len(rows),
            "hubs_by_main_hazard": counts,
            "gap_first_to_last_days": round(abs(rows[0]["days_per_year"] - rows[-1]["days_per_year"]), 2) if rows else None,
        },
        "hubs_not_ranked_incomplete_data": out["excluded_incomplete_data"],
        "method": _method(),
    }


def hub_breakdown(args: HubBreakdownInput) -> dict:
    ids = list(dict.fromkeys(args.hub_ids))
    years = list(range(int(settings.history_start[:4]), int(settings.history_end[:4]) + 1))
    key = "days" if args.by == "year" else "days_per_year"
    hubs = []
    for i in ids:
        r = service.by_id(i)
        rows = breakdown(r, args.hazard, args.by, years)
        top = sorted(rows, key=lambda x: -x[key])
        hubs.append({"hub": {k: getattr(r, k) for k in ("hub_id", "city", "state", "region")},
                     "periods": rows, "highest_period": top[0]["period"], "lowest_period": top[-1]["period"],
                     "difference_highest_minus_lowest_days": round(top[0][key] - top[-1][key], 2),
                     "annual_total_days_per_year": (r.total_days_per_year if args.hazard == "total"
                                                    else r.hazards[args.hazard].days_per_year)})
    result = {
        "hazard": args.hazard, "by": args.by,
        "values_mean": ("actual disruption days in each calendar year (weather days of that year plus FEMA "
                        "disasters that started that year, at their full days)") if args.by == "year" else
                       ("expected disruption days per year falling in each period (averages over the data); "
                        "the periods add up to the annual total"),
        **({"seasons_definition": SEASON_NOTE} if args.by == "season" else {}),
        "hubs": hubs,
        "small_sample_warning": ("only 5 years of weather data: year-to-year differences mostly reflect "
                                 "weather variability, not a trend" if args.by == "year" else
                                 "each month or season rests on few events in 5 years (see events_in_data); "
                                 "hurricane months rest on very few FEMA disasters"),
        "method": _method(),
    }
    if len(hubs) > 1:  # per period: which hub is higher and by how much
        result["comparison_by_period"] = {
            str(p["period"]): _leader({h["hub"]["city"]: h["periods"][j][key] for h in hubs})
            for j, p in enumerate(hubs[0]["periods"])}
    return result


VARIABLES = {  # tool name -> (column, unit, label)
    "snowfall_cm": ("snowfall_sum", "cm", "daily snowfall"),
    "rain_mm": ("rain_sum", "mm", "daily rain"),
    "precipitation_mm": ("precip_sum", "mm", "daily precipitation (rain + snow water)"),
    "max_temp_c": ("tmax", "C", "daily maximum temperature"),
    "min_temp_c": ("tmin", "C", "daily minimum temperature"),
    "max_gust_kmh": ("wind_gusts_max", "km/h", "daily maximum wind gust"),
}
_OPS = {">": lambda s, v: s > v, ">=": lambda s, v: s >= v, "<": lambda s, v: s < v, "<=": lambda s, v: s <= v}


def weather_stat(args: WeatherStatInput) -> dict:
    months = months_of(args.season, args.months)
    col, unit, label = VARIABLES[args.variable]
    rows, values = [], {}
    for i in dict.fromkeys(args.hub_ids):
        r = service.by_id(i)
        df = service.weather(i)
        d = pd.to_datetime(df["date"])
        mask = pd.Series(True, index=df.index)
        if args.year is not None:
            mask &= d.dt.year == args.year
        if months is not None:
            mask &= d.dt.month.isin(months)
        sel, s = df[mask], df.loc[mask, col]
        row = {"hub": {k: getattr(r, k) for k in ("hub_id", "city", "state")},
               "date_range": f"{d[mask].min().date()}..{d[mask].max().date()}" if len(sel) else None,
               "days_in_period": int(len(sel))}
        if args.statistic == "count_days":
            n = int(_OPS[args.condition_op](s, args.condition_value).sum())
            row.update(condition=f"{label} {args.condition_op} {args.condition_value:g} {unit}",
                       days_matching=n, percentage_of_days=round(100 * n / len(sel), 1) if len(sel) else None)
            # "Days with snowfall": any trace (> 0) and a meaningful amount (>= 1) differ a lot; give both.
            if args.condition_op == ">" and args.condition_value == 0 and unit in ("cm", "mm"):
                n1 = int((s >= 1).sum())
                row.update({f"days_with_at_least_1_{unit}": n1,
                            f"percentage_with_at_least_1_{unit}": round(100 * n1 / len(sel), 1) if len(sel) else None})
            values[r.city] = row["percentage_of_days"] or 0.0
        else:
            if len(sel):
                if args.statistic in ("max", "min"):
                    idx = s.idxmax() if args.statistic == "max" else s.idxmin()
                    row.update(value=round(float(s[idx]), 1), on_date=str(df.at[idx, "date"])[:10])
                else:
                    row.update(value=round(float(s.sum() if args.statistic == "sum" else s.mean()), 1))
            values[r.city] = row.get("value", 0.0)
        rows.append(row)
    result = {
        "variable": f"{label} ({unit})", "statistic": args.statistic,
        "period": (f"year {args.year}" if args.year else f"all years {settings.history_start[:4]}-"
                   f"{settings.history_end[:4]}") + ("" if months is None else f", {describe(months)} only"),
        **({"seasons_definition": SEASON_NOTE} if months else {}),
        "hubs": rows,
        "data_notes": "Open-Meteo daily reanalysis on a ~25 km grid, not a weather station; reanalysis "
                      "snowfall is likely ~30% low (see config/disruption.yaml)",
        "latest_full_year": METHOD["latest_full_year"],
    }
    if len(rows) > 1:
        result["comparison"] = _leader(values)
    return result


# name -> (input model, function, description). The description is what the LLM reads.
TOOLS: dict[str, tuple[type[BaseModel], callable, str]] = {
    "rank_hubs": (
        RankHubsInput, rank_hubs,
        "Rank the company's 50 distribution hubs by expected weather disruption days per year, "
        "overall or for one hazard, optionally within one US Census region and within a season or "
        "months, most or least exposed first. Returns per-hub totals, the main hazard, the top "
        "drivers, caveats and the method. Use it for 'which hubs are most exposed / safest', "
        "including 'in summer' or 'in January'.",
    ),
    "explain_hub": (
        ExplainHubInput, explain_hub,
        "Explain one hub's expected disruption days per year: its overall rank and position against "
        "the median hub, the split by hazard, the weather rules and FEMA disasters behind each hazard, "
        "and caveats. Use it for 'why is <hub> high/low', 'what drives <hub>', or 'tell me about <hub>'.",
    ),
    "compare_hubs": (
        CompareHubsInput, compare_hubs,
        "Compare 2 to 5 hubs side by side, on selected hazards or all of them, for the whole year or "
        "within a season or months: days per year, rank among all hubs, drivers and FEMA disasters per "
        "hazard, with pre-computed differences. Use it for 'compare X and Y' or 'is X riskier than Y "
        "(in hurricane season)'.",
    ),
    "hub_breakdown": (
        HubBreakdownInput, hub_breakdown,
        "Split 1 to 5 hubs' disruption days by month, by season, or by calendar year (2021-2025), "
        "overall or for one hazard, with the highest and lowest period and pre-computed differences "
        "between hubs per period. Use it for 'when in the year', 'which month is worst', 'by season', "
        "'month by month', or 'was 2024 worse than 2021'.",
    ),
    "weather_stat": (
        WeatherStatInput, weather_stat,
        "Statistics of the raw daily weather (not disruption days) for 1 to 5 hubs: number and "
        "percentage of days meeting a condition, or the sum, mean, max or min of snowfall, rain, "
        "precipitation, temperature or wind gusts, for a calendar year and/or season or months. "
        "Use it for 'what percentage of days had snowfall', 'how many days above 40 C', "
        "'total snowfall in January', 'strongest gust'.",
    ),
}


def tool_definitions() -> list[dict]:
    """Tool definitions for the Messages API, with strict JSON schemas generated from Pydantic.

    transform_schema converts the Pydantic schema to what strict mode supports: constraints such
    as minimum/maximum are moved into the description (Pydantic still enforces them in run_tool).
    """
    return [{"name": name, "description": desc, "input_schema": anthropic.transform_schema(model), "strict": True}
            for name, (model, _, desc) in TOOLS.items()]


def run_tool(name: str, raw_input: dict) -> tuple[dict, bool]:
    """Validate the LLM's arguments and run the tool. Returns (result, is_error)."""
    if name not in TOOLS:
        return {"error": f"unknown tool '{name}'"}, True
    model, fn, _ = TOOLS[name]
    try:
        args = model.model_validate(raw_input)
    except ValidationError as exc:
        # include_context=False: the context can hold exception objects that are not JSON-serialisable.
        return {"error": "invalid arguments",
                "details": exc.errors(include_url=False, include_context=False)}, True
    return fn(args), False
