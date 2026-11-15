"""Disruption-days engine: expected operating days lost per year, per hub and per hazard.

Deterministic (no LLM): the same data and the same config/disruption.yaml always give
the same numbers. Every number can be traced to the rule and the days that produced it.

Pipeline per hub:
  declarations channel -> counted FEMA disasters (rare events) + their date windows
  weather channel      -> classify each day by its worst rule -> group consecutive days
                          into events -> add recovery tails -> drop days already
                          covered by a counted declaration -> annualise
"""
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from app.config import DisruptionConfig, WeatherRule, load_scoring


# ---------------------------------------------------------------- output models

class Driver(BaseModel):
    source: str              # "weather" | "recovery" | "declarations"
    label: str               # the rule that produced these days
    occurrences: float       # trigger days, events or disasters in the data
    days_per_year: float     # contribution to the KPI


class HazardDisruption(BaseModel):
    hazard: str
    label: str
    days_per_year: float
    index: float = 0.0       # 100 x days / reference days (a typical hub's total); see config index
    tier: str = ""
    from_weather: float
    from_declarations: float
    events_per_year: float
    drivers: list[Driver]


class CountedDeclaration(BaseModel):
    disaster_number: int
    title: str
    incident_type: str
    declaration_date: str
    hazard: str
    days: float
    incident_begin_date: str | None = None


class DayRecord(BaseModel):
    """One disruption day (weather channel) or one counted disaster (declarations channel).

    The engine keeps these so any time breakdown (month, season, year) is a sum over the same
    records as the annual total: the months always add up to the year."""
    date: date
    hazard: str
    value: float     # weather: severity of that day; declarations: days counted for the disaster
    channel: str     # "weather" | "declarations"
    label: str       # the rule behind it (for drivers)
    event: str       # event id, to count distinct events behind a number


class HubDisruption(BaseModel):
    hub_id: str
    city: str
    state: str
    region: str
    total_days_per_year: float
    index: float = 0.0       # standardised: 100 = a typical hub (config index.reference_days)
    tier: str = ""           # low / moderate / elevated / high / very high
    hazards: dict[str, HazardDisruption]
    weather_complete: bool
    weather_coverage: float          # share of days in the window that have weather data
    weather_period: str | None
    weather_days_deduplicated: int   # weather days dropped because a declaration covers them
    declarations_counted: list[CountedDeclaration]
    # For time breakdowns (app/scoring/periods.py); not part of the API response.
    records: list[DayRecord] = Field(default=[], exclude=True)
    weather_years: float = Field(default=0.0, exclude=True)       # years of weather data
    declaration_years: float = Field(default=0.0, exclude=True)   # years of declarations


# ---------------------------------------------------------------- weather channel

_OPS = {">=": np.greater_equal, "<=": np.less_equal, "<": np.less}


def _rule_mask(df: pd.DataFrame, rule: WeatherRule) -> np.ndarray:
    """True on days where ALL of the rule's conditions hold."""
    mask = np.ones(len(df), dtype=bool)
    for c in rule.conditions:
        col = df[c.variable]
        if c.op == "in":
            mask &= col.isin(c.threshold).to_numpy()
        else:
            values = col.to_numpy(dtype=float)  # NaN compares False -> missing data never triggers
            with np.errstate(invalid="ignore"):
                mask &= _OPS[c.op](values, c.threshold)
    return mask


def classify_days(daily: pd.DataFrame, cfg: DisruptionConfig) -> pd.DataFrame:
    """One row per triggered day with its WORST rule (highest severity; ties -> first in config).

    A day that matches snow and gusts at once is counted once, not twice.
    """
    df = daily.sort_values("date").reset_index(drop=True)
    best = np.full(len(df), -1)
    best_sev = np.zeros(len(df))
    for i, rule in enumerate(cfg.weather_rules):
        hit = _rule_mask(df, rule) & (rule.severity > best_sev)
        best[hit], best_sev[hit] = i, rule.severity
    rows = []
    for idx in np.flatnonzero(best >= 0):
        rule = cfg.weather_rules[best[idx]]
        rows.append({"date": pd.Timestamp(df.at[idx, "date"]).date(), "hazard": rule.hazard,
                     "severity": rule.severity, "recovery_days": rule.recovery_days, "label": rule.label})
    return pd.DataFrame(rows, columns=["date", "hazard", "severity", "recovery_days", "label"])


def expand_events(triggered: pd.DataFrame, cfg: DisruptionConfig, last_date: date) -> pd.DataFrame:
    """Group consecutive trigger days into events and add one recovery tail per event.

    The tail belongs to the rule with the longest recovery in the event, starts the day
    after the event ends, and stops early if the next event starts (no double counting).
    Returns one row per disruption day: date, hazard, severity, kind, label, event_id.
    """
    if triggered.empty:
        return pd.DataFrame(columns=["date", "hazard", "severity", "kind", "label", "event_id"])
    t = triggered.sort_values("date").reset_index(drop=True)
    # A new event starts whenever the gap to the previous trigger day is not exactly 1 day.
    gaps = [None] + [(t.at[i, "date"] - t.at[i - 1, "date"]).days for i in range(1, len(t))]
    t["event_id"] = pd.Series([g != 1 for g in gaps]).cumsum()
    out = [{**r, "kind": "trigger"} for r in t[["date", "hazard", "severity", "label", "event_id"]].to_dict("records")]
    starts = t.groupby("event_id")["date"].min()
    for eid, ev in t.groupby("event_id"):
        top = ev.loc[ev["recovery_days"].idxmax()]
        if top["recovery_days"] == 0:
            continue
        next_start = starts.get(eid + 1)
        end = ev["date"].max()
        for k in range(1, int(top["recovery_days"]) + 1):
            d = end + timedelta(days=k)
            if (next_start is not None and d >= next_start) or d > last_date:
                break
            out.append({"date": d, "hazard": top["hazard"], "severity": cfg.recovery_severity,
                        "kind": "recovery", "label": f"Recovery after: {top['label']}", "event_id": eid})
    return pd.DataFrame(out)


# ---------------------------------------------------------------- declarations channel

def counted_declarations(decl: pd.DataFrame, cfg: DisruptionConfig) -> list[CountedDeclaration]:
    """Declarations that count in the rare-event channel (DR only, mapped incident types)."""
    type_to_rule = {t: r for r in cfg.declaration_rules for t in r.incident_types}
    d = decl[decl["declaration_type"].isin(cfg.declaration_types_counted) & decl["incident_type"].isin(type_to_rule)]
    d = d.drop_duplicates("disaster_number").sort_values("declaration_date", ascending=False)
    return [CountedDeclaration(disaster_number=int(r.disaster_number), title=str(r.title).title(),
                               incident_type=r.incident_type, declaration_date=r.declaration_date,
                               hazard=type_to_rule[r.incident_type].hazard,
                               days=type_to_rule[r.incident_type].days_per_event,
                               incident_begin_date=(str(r.incident_begin_date)[:10]
                                                    if getattr(r, "incident_begin_date", None) else None))
            for r in d.itertuples()]


def declaration_dates(decl: pd.DataFrame, counted: list[CountedDeclaration], cfg: DisruptionConfig) -> set[date]:
    """Dates covered by counted declarations: from 1 day before the incident start to N days after."""
    nums = {c.disaster_number for c in counted}
    covered = set()
    for r in decl[decl["disaster_number"].isin(nums)].itertuples():
        start = r.incident_begin_date or r.declaration_date
        if not start:
            continue
        begin = date.fromisoformat(str(start)[:10])
        for k in range(-1, cfg.declaration_dedup_window_days + 1):
            covered.add(begin + timedelta(days=k))
    return covered


# ---------------------------------------------------------------- per hub

def hub_disruption(hub: dict, daily: pd.DataFrame, decl: pd.DataFrame, cfg: DisruptionConfig,
                   window: tuple[date, date]) -> HubDisruption:
    """`daily`: this hub's weather rows; `decl`: this hub's county declarations; `window`: weather period."""
    counted = counted_declarations(decl, cfg)
    by_hazard: dict[str, dict] = defaultdict(lambda: {"weather": 0.0, "decl": 0.0, "events": set(),
                                                     "drivers": defaultdict(lambda: [0.0, 0.0])})

    # Declarations channel
    years_decl = cfg.declaration_window_years
    for c in counted:
        h = by_hazard[c.hazard]
        h["decl"] += c.days / years_decl
        drv = h["drivers"][("declarations", next(r.label for r in cfg.declaration_rules if r.hazard == c.hazard))]
        drv[0] += 1
        drv[1] += c.days / years_decl
    decl_events_per_year = defaultdict(float)
    records: list[DayRecord] = []
    for c in counted:
        decl_events_per_year[c.hazard] += 1 / years_decl
        # Placed on the incident start (the month it happened), else the declaration date.
        records.append(DayRecord(date=date.fromisoformat((c.incident_begin_date or c.declaration_date)[:10]),
                                 hazard=c.hazard, value=c.days, channel="declarations",
                                 label=next(r.label for r in cfg.declaration_rules if r.hazard == c.hazard),
                                 event=f"DR-{c.disaster_number}"))

    # Weather channel
    expected_days = (window[1] - window[0]).days + 1
    n_days = int(daily["date"].nunique()) if len(daily) else 0
    coverage = round(n_days / expected_days, 3)
    dedup_count = 0
    period = None
    years_wx = 0.0
    if n_days:
        dates = pd.to_datetime(daily["date"])
        period = f"{dates.min().date()}..{dates.max().date()}"
        years_wx = n_days / 365.25
        triggered = classify_days(daily, cfg)
        days = expand_events(triggered, cfg, last_date=dates.max().date())
        if not days.empty:
            covered = declaration_dates(decl, counted, cfg)
            keep = ~days["date"].isin(covered)
            dedup_count = int((~keep).sum())
            days = days[keep]
            for r in days.itertuples():
                records.append(DayRecord(date=r.date, hazard=r.hazard, value=r.severity, channel="weather",
                                         label=r.label,
                                         event=f"W-{r.event_id}"))
                h = by_hazard[r.hazard]
                h["weather"] += r.severity / years_wx
                h["events"].add(r.event_id)
                drv = h["drivers"][("recovery" if r.kind == "recovery" else "weather", r.label)]
                drv[0] += 1
                drv[1] += r.severity / years_wx
            for hz, h in by_hazard.items():
                h["events_per_year_wx"] = len(h["events"]) / years_wx

    hazards = {}
    for hz, label in cfg.hazards.items():
        h = by_hazard.get(hz)
        if h is None:
            hazards[hz] = HazardDisruption(hazard=hz, label=label, days_per_year=0, from_weather=0,
                                           from_declarations=0, events_per_year=0, drivers=[])
            continue
        drivers = sorted((Driver(source=s, label=l, occurrences=round(v[0], 3), days_per_year=round(v[1], 3))
                          for (s, l), v in h["drivers"].items()), key=lambda d: -d.days_per_year)
        hazards[hz] = HazardDisruption(
            hazard=hz, label=label,
            days_per_year=round(h["weather"] + h["decl"], 3),
            from_weather=round(h["weather"], 3), from_declarations=round(h["decl"], 3),
            events_per_year=round(h.get("events_per_year_wx", 0) + decl_events_per_year[hz], 3),
            drivers=drivers)

    for h in hazards.values():  # standardised index and tier per hazard (same reference as the total)
        h.index = cfg.index.index(h.days_per_year)
        h.tier = cfg.index.tier(h.index)
    total = round(sum(h.days_per_year for h in hazards.values()), 3)
    total_index = cfg.index.index(total)
    return HubDisruption(
        hub_id=hub["hub_id"], city=hub["city"], state=hub["state"], region=hub["region"],
        total_days_per_year=total, index=total_index, tier=cfg.index.tier(total_index),
        hazards=hazards, weather_complete=coverage >= cfg.min_weather_coverage,
        weather_coverage=coverage, weather_period=period,
        weather_days_deduplicated=dedup_count, declarations_counted=counted,
        records=records, weather_years=years_wx, declaration_years=years_decl)


def compute_all(daily: pd.DataFrame, decl: pd.DataFrame, hubs: pd.DataFrame, cfg: DisruptionConfig,
                window: tuple[date, date]) -> list[HubDisruption]:
    results = []
    for hub in hubs.to_dict("records"):
        results.append(hub_disruption(hub, daily[daily["hub_id"] == hub["hub_id"]],
                                      decl[decl["county_fips"] == hub["county_fips"]], cfg, window))
    return results


def rank(results: list[HubDisruption], hazard: str = "total", region: str | None = None,
         top_n: int = 10, months: frozenset[int] | None = None, order: str = "most") -> dict:
    """Rank hubs by expected disruption days/year. Hubs with incomplete weather data are not
    ranked against the others (their total would be understated); they are listed separately."""
    pool = [r for r in results if region is None or r.region.lower() == region.lower()]

    def value(r):
        if months is not None:  # days per year within those months (e.g. a season)
            from app.scoring.periods import period_days
            return period_days(r, hazard, months)
        return r.total_days_per_year if hazard == "total" else r.hazards[hazard].days_per_year

    sign = -1 if order == "most" else 1
    complete = sorted((r for r in pool if r.weather_complete), key=lambda r: (sign * value(r), r.hub_id))
    idx = load_scoring().index
    ranking = [{"rank": i, "hub_id": r.hub_id, "city": r.city, "state": r.state, "region": r.region,
                "days_per_year": value(r), "index": idx.index(value(r)), "tier": idx.tier(idx.index(value(r)))}
               for i, r in enumerate(complete[:top_n], 1)]
    return {"hazard": hazard, "region": region, "ranking": ranking,
            "excluded_incomplete_data": [r.hub_id for r in pool if not r.weather_complete]}
