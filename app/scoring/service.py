"""Current disruption results: stored in the database, cached in memory.

The scores live in the scoring tables (app/scoring/store.py): one 'active' scoring run per set of
rules and data. At startup the service restores the snapshot, makes sure the active run matches
the current rules and data (scoring and storing a new run if not, ~1 s), and loads that run.

The in-memory copy is only a cache of the active run. Every few seconds results() checks whether
another run became active (e.g. `python -m app.scoring.store --new` after editing the rules) and
reloads it, so new scores reach a running server without a restart.
"""
import time

from app import db
from app.config import load_hubs
from app.scoring import store
from app.scoring.disruption import HubDisruption

RELOAD_CHECK_SECONDS = 5.0

_RESULTS: list[HubDisruption] | None = None
_RUN_ID: int | None = None
_CHECKED_AT = 0.0
_WEATHER = None  # daily weather of all hubs (pandas DataFrame), for weather statistics


def load_results(restore: bool = True) -> list[HubDisruption]:
    global _RESULTS, _RUN_ID, _CHECKED_AT, _WEATHER
    if restore:
        db.restore_snapshot()
    _WEATHER = db.read_df("SELECT * FROM daily_weather ORDER BY hub_id, date")
    _RUN_ID = store.ensure_current()
    _RESULTS = store.load_run(_RUN_ID)
    _CHECKED_AT = time.monotonic()
    return _RESULTS


def _refresh_if_new_run() -> None:
    global _RESULTS, _RUN_ID, _CHECKED_AT
    if time.monotonic() - _CHECKED_AT < RELOAD_CHECK_SECONDS:
        return
    _CHECKED_AT = time.monotonic()
    active = store.active_run_id()
    if active is not None and active != _RUN_ID:
        _RESULTS, _RUN_ID = store.load_run(active), active


def results() -> list[HubDisruption]:
    if _RESULTS is None:
        return load_results()
    _refresh_if_new_run()
    return _RESULTS


def run_id() -> int | None:
    """The scoring run the current answers come from."""
    results()
    return _RUN_ID


def weather(hub_id: str):
    """Daily weather rows of one hub (date, tmax, tmin, rain_sum, snowfall_sum, ...)."""
    if _WEATHER is None:
        load_results()
    return _WEATHER[_WEATHER["hub_id"] == hub_id].reset_index(drop=True)


def by_id(hub_id: str) -> HubDisruption | None:
    return next((r for r in results() if r.hub_id == hub_id), None)


def hub_ids() -> list[str]:
    return [h.id for h in load_hubs()]
