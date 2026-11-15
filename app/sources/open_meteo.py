"""Open-Meteo historical weather (ERA5 reanalysis): daily values per hub.

Free tier: no key, non-commercial use only, ~10k calls/day per IP. A long request
counts as many calls (roughly one per 14 days per 10 variables), so 5 years for one
hub costs ~100 calls. Set OPEN_METEO_API_KEY to use the paid endpoint instead.
"""
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, model_validator

from app.config import Hub
from app.settings import settings
from app.sources.http_cache import cached_get_json

FREE_URL = "https://archive-api.open-meteo.com/v1/archive"
PAID_URL = "https://customer-archive-api.open-meteo.com/v1/archive"

# Open-Meteo variable -> our column name. Each one maps to a disruption mechanism
# (see docs/PLAN.md, "Parameters to keep").
VARIABLES = {
    "temperature_2m_max": "tmax",        # extreme heat: labour limits, equipment, reefer load
    "temperature_2m_min": "tmin",        # freezing / extreme cold
    "precipitation_sum": "precip_sum",   # heavy rain -> flooding
    "rain_sum": "rain_sum",
    "snowfall_sum": "snowfall_sum",      # cm; road and yard closures
    "wind_gusts_10m_max": "wind_gusts_max",  # km/h; dock and trailer operations stop
    "wind_speed_10m_max": "wind_speed_max",
    "weather_code": "weather_code",      # WMO code; 56/57/66/67 = freezing drizzle/rain (ice)
}


class OpenMeteoDaily(BaseModel):
    """Validates the response shape, so a change in the API fails loudly at ingest."""
    latitude: float
    longitude: float
    daily: dict[str, list[float | int | str | None]]

    @model_validator(mode="after")
    def check_columns(self):
        missing = {"time", *VARIABLES} - set(self.daily)
        if missing:
            raise ValueError(f"Open-Meteo response missing variables: {missing}")
        lengths = {len(v) for v in self.daily.values()}
        if len(lengths) != 1:
            raise ValueError(f"Open-Meteo daily arrays have different lengths: {lengths}")
        return self


def fetch_daily(hub: Hub, start: str, end: str, refresh: bool = False) -> tuple[pd.DataFrame, Path, bool]:
    params = {
        "latitude": hub.lat, "longitude": hub.lon,
        "start_date": start, "end_date": end,
        "daily": ",".join(VARIABLES), "timezone": "auto",
    }
    url = FREE_URL
    if settings.open_meteo_api_key:
        url, params["apikey"] = PAID_URL, settings.open_meteo_api_key
    raw, path, from_cache = cached_get_json("open_meteo", url, params, refresh=refresh)
    parsed = OpenMeteoDaily.model_validate(raw)
    df = pd.DataFrame(parsed.daily).rename(columns={"time": "date", **VARIABLES})
    df.insert(0, "hub_id", hub.id)
    return df, path, from_cache
