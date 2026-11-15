"""Current disruption results: stored in the database, cached in memory.

The scores live in the scoring tables (app/scoring/store.py): one 'active' scoring run per set of
rules and data. At startup the service restores the snapshot, makes sure the active run matches
the current rules and data (scoring and storing a new run if not, ~1 s), and loads that run.

The in-memory copy is only a cache of the active run. Every few seconds results() checks whether
another run became active (e.g. `python -m app.scoring.store --new` after editing the rules) and
reloads it, so new scores reach a running server without a restart.
"""
import threading
import time

from app import db
from app.config import load_hubs
from app.scoring import store
from app.scoring.disruption import HubDisruption

RELOAD_CHECK_SECONDS = 5.0

# (run_id, results) is replaced as ONE object, so a request thread can never see the results of
# one run labelled with the id of another. _LOCK makes concurrent requests reload a new run once.
_STATE: tuple[int, list[HubDisruption]] | None = None
_LOCK = threading.Lock()
_CHECKED_AT = 0.0
_WEATHER = None  # daily weather of all hubs (pandas DataFrame), for weather statistics


def load_results(restore: bool = True) -> list[HubDisruption]:
    global _STATE, _CHECKED_AT, _WEATHER
    with _LOCK:
        if restore:
            db.restore_snapshot()
        _WEATHER = db.read_df("SELECT * FROM daily_weather ORDER BY hub_id, date")
        run = store.ensure_current()
        _STATE = (run, store.load_run(run))
        _CHECKED_AT = time.monotonic()
        return _STATE[1]


def _refresh_if_new_run() -> None:
    global _STATE, _CHECKED_AT
    if time.monotonic() - _CHECKED_AT < RELOAD_CHECK_SECONDS:
        return
    with _LOCK:  # one thread reloads; the others keep serving the current state meanwhile
        if time.monotonic() - _CHECKED_AT < RELOAD_CHECK_SECONDS:
            return
        _CHECKED_AT = time.monotonic()
        active = store.active_run_id()
        if active is not None and (_STATE is None or active != _STATE[0]):
            _STATE = (active, store.load_run(active))


def _state() -> tuple[int, list[HubDisruption]]:
    if _STATE is None:
        load_results()
    else:
        _refresh_if_new_run()
    return _STATE


def results() -> list[HubDisruption]:
    return _state()[1]


def run_id() -> int | None:
    """The scoring run the current answers come from."""
    return _state()[0]


def weather(hub_id: str):
    """Daily weather rows of one hub (date, tmax, tmin, rain_sum, snowfall_sum, ...)."""
    if _WEATHER is None:
        load_results()
    return _WEATHER[_WEATHER["hub_id"] == hub_id].reset_index(drop=True)


def by_id(hub_id: str) -> HubDisruption | None:
    return next((r for r in results() if r.hub_id == hub_id), None)


def hub_ids() -> list[str]:
    return [h.id for h in load_hubs()]
