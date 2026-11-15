"""Turn raw daily weather and declarations into per-hub metrics (pure functions, no I/O)."""
import pandas as pd

from app.config import ScoringConfig


def weather_metrics(daily: pd.DataFrame, cfg: ScoringConfig) -> pd.DataFrame:
    """Annualised weather metrics per hub. `daily` has one row per hub per day.

    Days with a missing value count as 'no event' for that variable; coverage is
    reported via `n_days` so a short record is visible rather than hidden.
    """
    t = cfg.thresholds
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    flags = pd.DataFrame({
        "hub_id": d["hub_id"],
        "snow_day": d["snowfall_sum"].fillna(0) >= t["snow_day_cm"],
        "heavy_snow": d["snowfall_sum"].fillna(0) >= t["heavy_snow_cm"],
        "extreme_cold": d["tmin"] <= t["extreme_cold_c"],
        "freezing_rain": d["weather_code"].isin(cfg.thresholds["freezing_rain_codes"]) if "weather_code" in d else False,
        "heavy_precip": d["precip_sum"].fillna(0) >= t["heavy_precip_mm"],
        "high_gust": d["wind_gusts_max"] >= t["high_gust_kmh"],
        "extreme_heat": d["tmax"] >= t["extreme_heat_c"],
    })
    g = flags.groupby("hub_id")
    n_days = g.size()
    years = n_days / 365.25
    out = pd.DataFrame({
        "n_days": n_days,
        "snow_day_pct": 100 * g["snow_day"].sum() / n_days,
        "heavy_snow_days": g["heavy_snow"].sum() / years,
        "extreme_cold_days": g["extreme_cold"].sum() / years,
        "freezing_rain_days": g["freezing_rain"].sum() / years,
        "heavy_precip_days": g["heavy_precip"].sum() / years,
        "high_gust_days": g["high_gust"].sum() / years,
        "extreme_heat_days": g["extreme_heat"].sum() / years,
    })
    out["first_date"] = d.groupby("hub_id")["date"].min().dt.date.astype(str)
    out["last_date"] = d.groupby("hub_id")["date"].max().dt.date.astype(str)
    return out


def declaration_counts(decl: pd.DataFrame, hubs: pd.DataFrame, cfg: ScoringConfig) -> pd.DataFrame:
    """Count of counted-type declarations per hub per declarations component."""
    d = decl[decl["declaration_type"].isin(cfg.declaration_types_counted)]
    d = d.merge(hubs[["hub_id", "county_fips"]], on="county_fips")
    type_to_comp = {t: comp for comp, types in cfg.declaration_incident_types.items() for t in types}
    d = d.assign(component=d["incident_type"].map(type_to_comp)).dropna(subset=["component"])
    counts = d.pivot_table(index="hub_id", columns="component", values="disaster_number",
                           aggfunc="nunique", fill_value=0)
    counts = counts.reindex(index=hubs["hub_id"], columns=list(cfg.declaration_incident_types), fill_value=0)
    return counts.fillna(0).astype(int)


def declaration_examples(decl: pd.DataFrame, hubs: pd.DataFrame, cfg: ScoringConfig, hub_id: str) -> list[dict]:
    """The individual counted declarations behind a hub's score (for explanations)."""
    fips = hubs.loc[hubs["hub_id"] == hub_id, "county_fips"].iloc[0]
    type_to_comp = {t: comp for comp, types in cfg.declaration_incident_types.items() for t in types}
    d = decl[(decl["county_fips"] == fips) & decl["declaration_type"].isin(cfg.declaration_types_counted)]
    d = d[d["incident_type"].isin(type_to_comp)].sort_values("declaration_date", ascending=False)
    return [{"title": r.title.title(), "date": r.declaration_date, "type": r.incident_type,
             "disaster_number": int(r.disaster_number)} for r in d.itertuples()]
