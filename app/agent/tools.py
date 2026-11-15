"""Tools the agent can call. Plain Python over the deterministic engine: the LLM picks
the tool and the arguments, the numbers always come from here."""
import anthropic
from pydantic import BaseModel, ValidationError

import statistics

from app.agent.schemas import CompareHubsInput, ExplainHubInput, RankHubsInput
from app.config import load_scoring
from app.scoring import service
from app.scoring.disruption import rank
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
}


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


def _ranks(hazard: str) -> dict[str, int]:
    """hub_id -> rank among all hubs with complete data, for one hazard or the total."""
    out = rank(service.results(), hazard, None, 50)
    return {row["hub_id"]: row["rank"] for row in out["ranking"]}


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
        "method": METHOD,
    }


def compare_hubs(args: CompareHubsInput) -> dict:
    ids = list(dict.fromkeys(args.hub_ids))  # keep order, drop repeats
    hazards = list(dict.fromkeys(args.hazards)) if args.hazards else SINGLE_HAZARDS
    hubs = [service.by_id(i) for i in ids]
    ranks = {h: _ranks(h) for h in ["total", *hazards]}
    rows = [{
        "hub": {k: getattr(r, k) for k in ("hub_id", "city", "state", "region")},
        "total_days_per_year": r.total_days_per_year,
        "overall_rank": ranks["total"].get(r.hub_id),
        "hazards": {h: _hazard_detail(r, h, ranks[h], 2) for h in hazards},
        "caveats": _caveats(r),
    } for r in hubs]

    def leader(values: dict[str, float]) -> dict:
        top = sorted(values.items(), key=lambda kv: -kv[1])
        return {"highest": top[0][0], "lowest": top[-1][0],
                "difference_highest_minus_lowest_days": round(top[0][1] - top[-1][1], 2),
                "tied": top[0][1] == top[-1][1]}

    # Pre-computed differences: the model must not subtract by itself.
    comparison = {h: leader({r.city: r.hazards[h].days_per_year for r in hubs}) for h in hazards}
    if args.hazards:
        comparison["selected_hazards_combined"] = leader(
            {r.city: round(sum(r.hazards[h].days_per_year for h in hazards), 3) for r in hubs})
        for row, r in zip(rows, hubs):
            row["selected_hazards_days_per_year"] = round(sum(r.hazards[h].days_per_year for h in hazards), 3)
    comparison["total"] = leader({r.city: r.total_days_per_year for r in hubs})
    return {"hubs": rows, "comparison": comparison, "hubs_ranked": len(ranks["total"]), "method": METHOD}


def rank_hubs(args: RankHubsInput) -> dict:
    out = rank(service.results(), args.hazard, args.region, args.top_n)
    labels = load_scoring().hazards
    rows = []
    for row in out["ranking"]:
        r = service.by_id(row["hub_id"])
        hazards = sorted(r.hazards.values(), key=lambda h: -h.days_per_year)
        main = hazards[0] if args.hazard == "total" else r.hazards[args.hazard]
        rows.append({
            **{k: row[k] for k in ("rank", "hub_id", "city", "state", "region", "days_per_year")},
            "main_hazard": main.label,
            "by_hazard": {h.hazard: h.days_per_year for h in hazards if h.days_per_year > 0},
            "top_drivers": [{"label": d.label, "days_per_year": d.days_per_year} for d in main.drivers[:2]],
            **({"caveat": "high-wind days likely overstated (coastal grid cell partly over water)"}
               if r.hub_id in COASTAL_WIND_HUBS and r.hazards["high_wind"].days_per_year > 0 else {}),
        })
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["main_hazard"]] = counts.get(row["main_hazard"], 0) + 1
    return {
        "hazard": args.hazard if args.hazard == "total" else f"{args.hazard} ({labels[args.hazard]})",
        "region": args.region,
        "ranking": rows,
        # Pre-computed so the model never has to count or subtract by itself.
        "summary": {
            "hubs_returned": len(rows),
            "hubs_by_main_hazard": counts,
            "gap_first_to_last_days": round(rows[0]["days_per_year"] - rows[-1]["days_per_year"], 2) if rows else None,
        },
        "hubs_not_ranked_incomplete_data": out["excluded_incomplete_data"],
        "method": METHOD,
    }


# name -> (input model, function, description). The description is what the LLM reads.
TOOLS: dict[str, tuple[type[BaseModel], callable, str]] = {
    "rank_hubs": (
        RankHubsInput, rank_hubs,
        "Rank the company's 50 distribution hubs by expected weather disruption days per year, "
        "overall or for one hazard, optionally within one US Census region. Returns per-hub totals, "
        "the main hazard, the top drivers, caveats and the method. Use it for any question about "
        "which hubs are most exposed, most dangerous, or highest risk.",
    ),
    "explain_hub": (
        ExplainHubInput, explain_hub,
        "Explain one hub's expected disruption days per year: its overall rank and position against "
        "the median hub, the split by hazard, the weather rules and FEMA disasters behind each hazard, "
        "and caveats. Use it for 'why is <hub> high/low', 'what drives <hub>', or 'tell me about <hub>'.",
    ),
    "compare_hubs": (
        CompareHubsInput, compare_hubs,
        "Compare 2 to 5 hubs side by side, on selected hazards or all of them: days per year, rank "
        "among all hubs, drivers and FEMA disasters per hazard, with pre-computed differences. Use it "
        "for 'compare X and Y' or 'is X riskier than Y'.",
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
        return {"error": "invalid arguments", "details": exc.errors(include_url=False)}, True
    return fn(args), False
