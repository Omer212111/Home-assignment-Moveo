"""Offline tests for the ingestion path (no network: API responses are faked)."""
import pytest

from app import db, ingest
from app.config import load_hubs
from app.settings import settings
from app.sources import open_meteo
from app.sources.http_cache import SourceError


def fake_open_meteo(n_days=3, **overrides):
    daily = {"time": [f"2025-01-0{i + 1}" for i in range(n_days)]}
    for var in open_meteo.VARIABLES:
        daily[var] = [1.0] * n_days
    daily["weather_code"] = [3] * n_days
    daily.update(overrides)
    return {"latitude": 39.7, "longitude": -105.0, "daily": daily}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "raw_cache_dir", tmp_path / "raw")
    return tmp_path


def test_open_meteo_parses_and_renames(monkeypatch, tmp_db):
    monkeypatch.setattr(open_meteo, "cached_get_json",
                        lambda *a, **k: (fake_open_meteo(), tmp_db / "raw" / "x.json", False))
    hub = next(h for h in load_hubs() if h.id == "denver")
    df, _, _ = open_meteo.fetch_daily(hub, "2025-01-01", "2025-01-03")
    assert list(df.columns) == ["hub_id", "date", *open_meteo.VARIABLES.values()]
    assert len(df) == 3 and (df["hub_id"] == "denver").all()


def test_open_meteo_rejects_missing_variable(monkeypatch, tmp_db):
    bad = fake_open_meteo()
    del bad["daily"]["snowfall_sum"]
    monkeypatch.setattr(open_meteo, "cached_get_json", lambda *a, **k: (bad, tmp_db / "x.json", False))
    hub = load_hubs()[0]
    with pytest.raises(ValueError, match="snowfall_sum"):
        open_meteo.fetch_daily(hub, "2025-01-01", "2025-01-03")


def test_weather_ingest_stores_rows_and_stops_on_rate_limit(monkeypatch, tmp_db):
    calls = []

    def fake(hub, start, end, refresh=False):
        calls.append(hub.id)
        if len(calls) == 2:
            raise SourceError("Daily API request limit exceeded")
        import pandas as pd
        d = fake_open_meteo()["daily"]
        df = pd.DataFrame(d).rename(columns={"time": "date", **open_meteo.VARIABLES})
        df.insert(0, "hub_id", hub.id)
        return df, settings.raw_cache_dir / "open_meteo" / "f.json", False

    monkeypatch.setattr(open_meteo, "fetch_daily", fake)
    hubs = load_hubs()[:5]
    with db.connect() as conn:
        failures = ingest.ingest_weather(conn, hubs, "2025-01-01", "2025-01-03", refresh=False)
        rows = conn.execute("SELECT COUNT(*) FROM daily_weather").fetchone()[0]
        errors = conn.execute("SELECT COUNT(*) FROM ingest_runs WHERE status='error'").fetchone()[0]
    assert calls == [hubs[0].id, hubs[1].id]   # stopped after the rate limit, did not burn more quota
    assert failures == [hubs[1].id]
    assert rows == 3 and errors == 1


def test_ingestion_triggers_a_scoring_run_only_when_it_should(tmp_db, monkeypatch, capsys):
    """The write path: a successful ingest scores; failures or no snapshot never replace the scores."""
    from app.scoring import store
    db.restore_snapshot()
    assert ingest.score_after_ingest(["denver: rate limited"], snapshot_written=True) is None
    assert store.active_run_id() is None                      # incomplete data never becomes the scores
    assert ingest.score_after_ingest([], snapshot_written=False) is None

    first = ingest.score_after_ingest([], snapshot_written=True)
    assert first is not None and store.active_run_id() == first
    assert ingest.score_after_ingest([], snapshot_written=True) == first   # same data: no new run
    assert "unchanged" in capsys.readouterr().out

    monkeypatch.setattr(store, "data_fingerprint", lambda: "new-data")    # ingestion changed the data
    second = ingest.score_after_ingest([], snapshot_written=True)
    assert second != first and store.active_run_id() == second
    assert f"new active run {second} (previous {first})" in capsys.readouterr().out
