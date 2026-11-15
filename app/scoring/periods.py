"""Time breakdowns of the disruption KPI: by month, season, year, or any set of months.

All functions sum the same per-hub records the annual total is built from (HubDisruption.records):
- weather records are averaged over the weather years (2021-2025),
- declaration records over the declaration window (20 years),
so "days per year in winter" + the other three seasons = the annual total.
"""
from collections import defaultdict

from app.config import load_scoring
from app.scoring.disruption import HubDisruption

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
# Meteorological seasons (an explicit assumption, stated in every result that uses them).
SEASONS: dict[str, frozenset[int]] = {
    "winter": frozenset({12, 1, 2}),
    "spring": frozenset({3, 4, 5}),
    "summer": frozenset({6, 7, 8}),
    "autumn": frozenset({9, 10, 11}),
}
SEASON_NOTE = "meteorological seasons: winter Dec-Feb, spring Mar-May, summer Jun-Aug, autumn Sep-Nov"


def months_of(season: str | None, months: list[int] | None) -> frozenset[int] | None:
    """The months a period covers, or None for the whole year."""
    if season and months:
        raise ValueError("give either season or months, not both")
    if season:
        return SEASONS[season]
    if months:
        return frozenset(months)
    return None


def describe(months: frozenset[int] | None) -> str:
    if months is None:
        return "whole year"
    for name, m in SEASONS.items():
        if m == months:
            return f"{name} ({', '.join(MONTH_NAMES[i - 1] for i in sorted(m, key=lambda x: (x < 12, x)))})"
    return "months: " + ", ".join(MONTH_NAMES[i - 1] for i in sorted(months))


def _weight(r: HubDisruption, rec) -> float:
    """A record's contribution to the expected days per year."""
    years = r.weather_years if rec.channel == "weather" else r.declaration_years
    return rec.value / years if years else 0.0


def _match(rec, hazard: str) -> bool:
    return hazard == "total" or rec.hazard == hazard


def period_days(r: HubDisruption, hazard: str, months: frozenset[int] | None) -> float:
    """Expected disruption days per year for one hazard (or total) within the given months."""
    return round(sum(_weight(r, x) for x in r.records
                     if _match(x, hazard) and (months is None or x.date.month in months)), 3)


def period_view(r: HubDisruption, months: frozenset[int] | None, n_drivers: int = 2) -> dict[str, dict]:
    """Per hazard within a period: days per year, events behind it, and the top drivers."""
    out: dict[str, dict] = defaultdict(lambda: {"days": 0.0, "events": set(), "drivers": defaultdict(float),
                                                "from_weather": 0.0, "from_declarations": 0.0})
    for x in r.records:
        if months is not None and x.date.month not in months:
            continue
        w = _weight(r, x)
        h = out[x.hazard]
        h["days"] += w
        h["events"].add(x.event)
        h["drivers"][x.label] += w
        h["from_weather" if x.channel == "weather" else "from_declarations"] += w
    return {hz: {"days_per_year": round(h["days"], 3), "events_in_data": len(h["events"]),
                 "from_weather": round(h["from_weather"], 3), "from_fema_declarations": round(h["from_declarations"], 3),
                 "top_drivers": [{"label": k, "days_per_year": round(v, 3)}
                                 for k, v in sorted(h["drivers"].items(), key=lambda kv: -kv[1])[:n_drivers]]}
            for hz, h in out.items()}


def breakdown(r: HubDisruption, hazard: str, by: str, years: list[int]) -> list[dict]:
    """One row per month / season / year.

    month, season: expected days per year in that period (averages, sums to the annual total).
    year: actual days in that calendar year: weather days of that year plus counted disasters whose
          incident started that year, at their full days (not averaged)."""
    rows = []
    idx = load_scoring().index
    if by in ("month", "season"):
        periods = ([(MONTH_NAMES[m - 1], frozenset({m})) for m in range(1, 13)] if by == "month"
                   else list(SEASONS.items()))
        for name, months in periods:
            recs = [x for x in r.records if _match(x, hazard) and x.date.month in months]
            by_hz = defaultdict(float)
            for x in recs:
                by_hz[x.hazard] += _weight(r, x)
            days = round(sum(by_hz.values()), 3)
            i = idx.index(days, len(months))
            rows.append({"period": name, "days_per_year": days, "index": i, "tier": idx.tier(i),
                         "events_in_data": len({x.event for x in recs}),
                         "main_hazard": max(by_hz, key=by_hz.get) if by_hz else None})
    else:
        for y in years:
            recs = [x for x in r.records if _match(x, hazard) and x.date.year == y]
            by_hz = defaultdict(float)
            for x in recs:
                by_hz[x.hazard] += x.value
            days = round(sum(by_hz.values()), 3)
            i = idx.index(days)
            rows.append({"period": y, "days": days, "index": i, "tier": idx.tier(i),
                         "events": len({x.event for x in recs}),
                         "main_hazard": max(by_hz, key=by_hz.get) if by_hz else None})
    return rows


def monthly_scores(r: HubDisruption, hazards: list[str]) -> list[dict]:
    """Days per year and index for every month, per hazard and for the total (one row each)."""
    idx = load_scoring().index
    out = []
    for hz in ["total", *hazards]:
        for m in range(1, 13):
            days = period_days(r, hz, frozenset({m}))
            i = idx.index(days, 1)
            out.append({"hazard": hz, "month": m, "days_per_year": days, "index": i, "tier": idx.tier(i)})
    return out
