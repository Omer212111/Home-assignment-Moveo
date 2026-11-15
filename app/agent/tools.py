"""Tools the agent can call. Plain Python over the deterministic engine: the LLM picks
the tool and the arguments, the numbers always come from here."""
import anthropic
from pydantic import BaseModel, ValidationError

from app.agent.schemas import RankHubsInput
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
