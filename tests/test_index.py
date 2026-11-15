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
