"""Current disruption results, computed once from the database and kept in memory.

50 hubs x 1,826 days takes about a second, so recomputing at startup (or after
a data refresh) is simpler than storing results in another table.
"""
from datetime import date

from app import db
from app.config import load_hubs, load_scoring
from app.scoring.disruption import HubDisruption, compute_all
from app.settings import settings

_RESULTS: list[HubDisruption] | None = None
_WEATHER = None  # daily weather of all hubs (pandas DataFrame), for weather statistics


def load_results(restore: bool = True) -> list[HubDisruption]:
    global _RESULTS, _WEATHER
    if restore:
        db.restore_snapshot()
    window = (date.fromisoformat(settings.history_start), date.fromisoformat(settings.history_end))
    _WEATHER = db.read_df("SELECT * FROM daily_weather ORDER BY hub_id, date")
    _RESULTS = compute_all(_WEATHER,
                           db.read_df("SELECT * FROM disaster_declarations"),
                           db.read_df("SELECT * FROM hubs ORDER BY pop_rank"), load_scoring(), window)
    return _RESULTS


def results() -> list[HubDisruption]:
    return _RESULTS if _RESULTS is not None else load_results()


def weather(hub_id: str):
    """Daily weather rows of one hub (date, tmax, tmin, rain_sum, snowfall_sum, ...)."""
    if _WEATHER is None:
        load_results()
    return _WEATHER[_WEATHER["hub_id"] == hub_id].reset_index(drop=True)


def by_id(hub_id: str) -> HubDisruption | None:
    return next((r for r in results() if r.hub_id == hub_id), None)


def hub_ids() -> list[str]:
    return [h.id for h in load_hubs()]
