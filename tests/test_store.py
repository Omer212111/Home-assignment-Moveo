"""Scores in the database: a stored run reloads exactly, runs are reused or replaced correctly."""
import pytest

from app import db
from app.scoring import service, store


@pytest.fixture(scope="module", autouse=True)
def snapshot():
    db.restore_snapshot()


def test_a_stored_run_reloads_exactly_what_the_engine_computed():
    computed = store._compute()
    sha, yaml_text = store.rules_identity()
    run = store.save_run(computed, sha, yaml_text, store.data_fingerprint())
    loaded = store.load_run(run)
    assert [r.hub_id for r in loaded] == [r.hub_id for r in computed]
    for a, b in zip(computed, loaded):
        assert a.model_dump() == b.model_dump()          # KPI, hazards, drivers, declarations
        key = lambda x: (x.date, x.hazard, x.event)
        assert sorted(a.records, key=key) == sorted(b.records, key=key)   # every disruption day
        assert (a.weather_years, a.declaration_years) == (b.weather_years, b.declaration_years)


def test_ensure_current_reuses_the_active_run_until_rules_or_data_change(monkeypatch):
    first = store.ensure_current()
    assert store.ensure_current() == first               # nothing changed: no new run
    sha, text = store.rules_identity()
    monkeypatch.setattr(store, "rules_identity", lambda: ("edited-rules", text + "\n# edited"))
    second = store.ensure_current()
    assert second != first                               # rules changed: scored again
    runs = {r["run_id"]: r["status"] for r in store.list_runs()}
    assert runs[second] == "active" and runs[first] == "superseded"
    assert sum(1 for s in runs.values() if s == "active") == 1


def test_running_server_picks_up_a_new_run_without_restart(monkeypatch):
    service.load_results(restore=False)
    old = service.run_id()
    new = store.new_run()
    monkeypatch.setattr(service, "RELOAD_CHECK_SECONDS", 0.0)
    assert service.run_id() == new != old


def test_diff_between_runs_lists_changed_hubs(monkeypatch):
    a = store.new_run()
    # Simulate a rule change that doubles Denver's winter days in a new run.
    results = store.load_run(a)
    den = next(r for r in results if r.hub_id == "denver")
    den.hazards["winter"].days_per_year *= 2
    den.total_days_per_year += den.hazards["winter"].days_per_year / 2
    sha, text = store.rules_identity()
    b = store.save_run(results, sha, text, store.data_fingerprint())
    changes = store.diff_runs(a, b)
    assert {(c["hub_id"], c["hazard"]) for c in changes} == {("denver", "winter"), ("denver", "total")}
    assert all(c["change"] > 0 for c in changes)
