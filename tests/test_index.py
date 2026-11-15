"""Standardised index (100 = a typical hub) and tiers."""
import sqlite3

import pytest
from pydantic import ValidationError

from app import db
from app.agent.tools import run_tool
from app.config import IndexConfig, load_scoring
from app.scoring import service

P = {"season": None, "months": None}


@pytest.fixture(scope="module", autouse=True)
def scores():
    service.load_results(restore=True)


def test_index_is_days_over_the_fixed_reference_and_hazards_add_up():
    ref = load_scoring().index.reference_days
    for r in service.results():
        assert r.index == pytest.approx(100 * r.total_days_per_year / ref, abs=0.06)
        assert sum(h.index for h in r.hazards.values()) == pytest.approx(r.index, abs=0.5)


def test_known_hubs_and_tier_boundaries():
    dallas, boston, san_diego = (service.by_id(h) for h in ("dallas", "boston", "san_diego"))
    assert (round(dallas.index), dallas.tier) == (125, "elevated")
    assert boston.tier == "very high" and san_diego.tier == "low"
    idx = load_scoring().index
    assert [idx.tier(x) for x in (0, 49.9, 50, 99.9, 100, 150, 199.9, 200, 500)] == \
        ["low", "low", "moderate", "moderate", "elevated", "high", "high", "very high", "very high"]


def test_tier_config_is_validated():
    with pytest.raises(ValidationError):
        IndexConfig(reference_days=5, tiers=[{"name": "a", "min": 10}])               # must start at 0
    with pytest.raises(ValidationError):
        IndexConfig(reference_days=5, tiers=[{"name": "a", "min": 0}, {"name": "b", "min": 0}])


def test_tools_and_api_carry_index_and_tier():
    res, _ = run_tool("rank_hubs", {**P, "hazard": "total", "region": None, "top_n": 3, "order": "most"})
    assert all({"index", "tier"} <= set(r) for r in res["ranking"])
    assert res["ranking"][0]["tier"] == "very high"
    res, _ = run_tool("explain_hub", {"hub_id": "dallas"})
    assert res["tier"] == "elevated" and "tier" in res["hazards_by_contribution"]["flood"]
    res, _ = run_tool("compare_hubs", {**P, "hub_ids": ["miami", "houston"], "hazards": ["tropical"]})
    assert all("index" in h and "index" in h["hazards"]["tropical"] for h in res["hubs"])
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/rankings", params={"top_n": 1}).json()["ranking"][0]["tier"] == "very high"
        assert c.get("/hubs/dallas").json()["tier"] == "elevated"


def test_old_scoring_tables_are_rebuilt_on_schema_upgrade(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)   # a version-1 database: scoring tables without index columns
    conn.execute("CREATE TABLE hub_results (run_id INTEGER, hub_id TEXT, total_days_per_year REAL)")
    conn.execute("CREATE TABLE conversations (session_id TEXT, turn INTEGER, role TEXT, content TEXT, "
                 "created_at TEXT, PRIMARY KEY (session_id, turn))")
    conn.execute("INSERT INTO conversations VALUES ('s', 0, 'user', 'hi', 'now')")
    conn.commit(); conn.close()
    monkeypatch.setattr(db.settings, "db_path", path)
    with db.connect() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(hub_results)")]
        kept = c.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    assert "index_value" in cols and "tier" in cols
    assert kept == 1                                     # chat history is not touched


def test_monthly_index_is_pro_rated_and_averages_to_the_annual_index():
    from app.scoring.periods import breakdown
    for hub in ("denver", "miami", "dallas"):
        r = service.by_id(hub)
        months = breakdown(r, "total", "month", [])
        assert sum(m["index"] for m in months) / 12 == pytest.approx(r.index, abs=0.2)
        seasons = breakdown(r, "total", "season", [])
        assert sum(s["index"] for s in seasons) / 4 == pytest.approx(r.index, abs=0.2)
    mar = next(m for m in breakdown(service.by_id("denver"), "total", "month", []) if m["period"] == "Mar")
    assert (round(mar["index"]), mar["tier"]) == (624, "very high")   # 2.52 days vs 4.85 / 12


def test_period_rankings_and_comparisons_use_the_pro_rated_reference():
    res, _ = run_tool("rank_hubs", {**P, "hazard": "total", "region": None, "top_n": 1, "order": "most",
                                    "season": "summer"})
    top = res["ranking"][0]
    assert top["index"] == pytest.approx(100 * top["days_per_year"] / (4.85 * 3 / 12), abs=0.1)
    res, _ = run_tool("compare_hubs", {"hub_ids": ["miami", "houston"], "hazards": ["tropical"],
                                       "season": None, "months": [6, 7, 8, 9, 10, 11]})
    h = res["hubs"][0]
    assert h["index"] == pytest.approx(100 * h["total_days_per_year"] / (4.85 * 6 / 12), abs=0.1)


def test_monthly_scores_table_matches_the_breakdown():
    from app.scoring import store
    from app.scoring.periods import breakdown
    run = service.run_id()
    with db.connect() as conn:
        rows = conn.execute("SELECT month, days_per_year, index_value, tier FROM monthly_scores "
                            "WHERE run_id = ? AND hub_id = 'denver' AND hazard = 'total' ORDER BY month",
                            (run,)).fetchall()
        n = conn.execute("SELECT COUNT(*) FROM monthly_scores WHERE run_id = ?", (run,)).fetchone()[0]
    expected = breakdown(service.by_id("denver"), "total", "month", [])
    assert [(r[1], r[2], r[3]) for r in rows] == [(e["days_per_year"], e["index"], e["tier"]) for e in expected]
    assert n == 50 * 7 * 12   # hubs x (6 hazards + total) x months
