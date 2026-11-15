"""Deterministic scoring engine. No LLM involved: same inputs + same scoring.yaml = same scores.

Missing data is handled explicitly: a component without data is left out and the
weights of the remaining components are re-normalised; `coverage` (0-1) reports how
much of the intended weight was actually available, so the agent can say so.
"""
import json
from datetime import datetime, timezone
from typing import Literal

import pandas as pd
from pydantic import BaseModel

from app import db
from app.config import ScoringConfig, load_scoring, scoring_version
from app.scoring.metrics import declaration_counts, weather_metrics

HazardKey = Literal["composite", "winter", "tropical", "flood", "severe_storm", "heat"]


class ComponentScore(BaseModel):
    name: str
    description: str
    source: Literal["metric", "declarations"]
    value: float | None          # raw value (None = no data)
    cap: float
    normalized: float | None     # 0-100
    weight: float                # weight inside the hazard


class HazardScore(BaseModel):
    hazard: str
    label: str
    weight: float                # weight inside the composite
    score: float | None          # 0-100, None if no component had data
    coverage: float              # share of component weight that had data
    components: list[ComponentScore]


class HubScore(BaseModel):
    hub_id: str
    city: str
    state: str
    region: str
    score: float | None          # composite 0-100
    tier: str
    coverage: float              # share of total intended weight that had data
    hazards: dict[str, HazardScore]
    weather_period: str | None   # e.g. "2021-01-01..2025-12-31"


def tier_for(score: float | None, cfg: ScoringConfig) -> str:
    if score is None:
        return "Unknown"
    for t in cfg.tiers:  # sorted high -> low
        if score >= t.min_score:
            return t.name
    return cfg.tiers[-1].name


def _weighted(items: list[tuple[float | None, float]]) -> tuple[float | None, float]:
    """Weighted mean over items with data; returns (score, coverage)."""
    total = sum(w for _, w in items)
    have = [(v, w) for v, w in items if v is not None]
    if not have:
        return None, 0.0
    wsum = sum(w for _, w in have)
    return sum(v * w for v, w in have) / wsum, wsum / total


def compute_scores(daily: pd.DataFrame, decl: pd.DataFrame, hubs: pd.DataFrame,
                   cfg: ScoringConfig) -> list[HubScore]:
    wx = weather_metrics(daily, cfg) if len(daily) else pd.DataFrame()
    dc = declaration_counts(decl, hubs, cfg)
    results = []
    for hub in hubs.itertuples():
        hazards = {}
        for key, hz in cfg.hazards.items():
            comps = []
            for c in hz.components:
                if c.source == "metric":
                    value = float(wx.at[hub.hub_id, c.name]) if hub.hub_id in wx.index else None
                else:
                    value = float(dc.at[hub.hub_id, c.name])
                norm = None if value is None else round(min(value / c.cap, 1.0) * 100, 1)
                comps.append(ComponentScore(name=c.name, description=c.description, source=c.source,
                                            value=None if value is None else round(value, 2),
                                            cap=c.cap, normalized=norm, weight=c.weight))
            score, cov = _weighted([(c.normalized, c.weight) for c in comps])
            hazards[key] = HazardScore(hazard=key, label=hz.label, weight=hz.weight,
                                       score=None if score is None else round(score, 1),
                                       coverage=round(cov, 2), components=comps)
        composite, _ = _weighted([(h.score, h.weight) for h in hazards.values()])
        coverage = sum(h.weight * h.coverage for h in hazards.values())
        period = None
        if hub.hub_id in wx.index:
            period = f"{wx.at[hub.hub_id, 'first_date']}..{wx.at[hub.hub_id, 'last_date']}"
        composite = None if composite is None else round(composite, 1)
        results.append(HubScore(hub_id=hub.hub_id, city=hub.city, state=hub.state, region=hub.region,
                                score=composite, tier=tier_for(composite, cfg), coverage=round(coverage, 2),
                                hazards=hazards, weather_period=period))
    return results


# ---- In-process store of current scores (cheap to recompute: 50 hubs, milliseconds) ----

_CURRENT: list[HubScore] | None = None


def load_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return (db.read_df("SELECT * FROM daily_weather"),
            db.read_df("SELECT * FROM disaster_declarations"),
            db.read_df("SELECT * FROM hubs ORDER BY pop_rank"))


def current_scores(refresh: bool = False) -> list[HubScore]:
    global _CURRENT
    if _CURRENT is None or refresh:
        daily, decl, hubs = load_inputs()
        _CURRENT = compute_scores(daily, decl, hubs, load_scoring())
    return _CURRENT


def compute_and_store_scores() -> int:
    """Recompute and append to hub_scores (history kept for change detection later)."""
    scores = current_scores(refresh=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    version = scoring_version()
    rows = []
    for s in scores:
        rows.append((s.hub_id, "composite", s.score, s.tier, json.dumps({"coverage": s.coverage}),
                     version, s.weather_period, now))
        for h in s.hazards.values():
            rows.append((s.hub_id, h.hazard, h.score, tier_for(h.score, load_scoring()),
                         h.model_dump_json(), version, s.weather_period, now))
    with db.connect() as conn:
        conn.executemany("INSERT INTO hub_scores VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(scores)


def rank_hubs(hazard: str = "composite", region: str | None = None, top_n: int = 10) -> list[dict]:
    """Deterministic ranking. Hubs with no data for the hazard go last (never silently dropped)."""
    out = []
    for s in current_scores():
        if region and s.region.lower() != region.lower():
            continue
        if hazard == "composite":
            score, cov = s.score, s.coverage
        else:
            h = s.hazards[hazard]
            score, cov = h.score, h.coverage
        out.append({"hub_id": s.hub_id, "city": s.city, "state": s.state, "region": s.region,
                    "score": score, "tier": tier_for(score, load_scoring()), "coverage": cov})
    out.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["hub_id"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out[:top_n]
