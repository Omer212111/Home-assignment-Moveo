"""Offline tests for the explain_hub and compare_hubs tools (slice 3)."""
import pytest

from app.agent.tools import run_tool
from app.scoring import service
from app.scoring.disruption import rank
from evals.checks import args_mismatch


@pytest.fixture(scope="module", autouse=True)
def scores():
    service.load_results(restore=True)


def test_explain_hub_gives_rank_context_and_matches_engine():
    res, err = run_tool("explain_hub", {"hub_id": "dallas"})
    assert not err
    r = service.by_id("dallas")
    expected_rank = next(x["rank"] for x in rank(service.results(), "total", None, 50)["ranking"]
                         if x["hub_id"] == "dallas")
    assert res["context"]["overall_rank"] == expected_rank
    assert res["total_days_per_year"] == r.total_days_per_year
    # hazards ordered by contribution and summing to the hub total
    days = [h["days_per_year"] for h in res["hazards_by_contribution"].values()]
    assert days == sorted(days, reverse=True)
    assert sum(days) == pytest.approx(r.total_days_per_year, abs=0.01)
    assert res["context"]["difference_vs_median_days"] == pytest.approx(
        r.total_days_per_year - res["context"]["median_hub_days_per_year"], abs=0.01)


def test_explain_hub_rejects_unknown_hub():
    res, err = run_tool("explain_hub", {"hub_id": "paris"})
    assert err and res["error"] == "invalid arguments"


def test_compare_hubs_selected_hazards_with_precomputed_differences():
    res, err = run_tool("compare_hubs", {"hub_ids": ["miami", "houston"], "hazards": ["tropical", "flood"], "season": None, "months": None})
    assert not err
    assert [h["hub"]["hub_id"] for h in res["hubs"]] == ["miami", "houston"]
    assert set(res["hubs"][0]["hazards"]) == {"tropical", "flood"}
    m, h = service.by_id("miami"), service.by_id("houston")
    trop = res["comparison"]["tropical"]
    assert trop["difference_highest_minus_lowest_days"] == pytest.approx(
        abs(m.hazards["tropical"].days_per_year - h.hazards["tropical"].days_per_year), abs=0.01)
    assert "selected_hazards_combined" in res["comparison"] and "total" in res["comparison"]
    # FEMA disasters behind the hurricane number are listed, and flood says it is weather-only
    assert res["hubs"][0]["hazards"]["tropical"]["fema_disasters_counted"]
    assert "rainfall" in res["hubs"][0]["hazards"]["flood"]["built_from"]


def test_compare_hubs_all_hazards_and_limits():
    res, err = run_tool("compare_hubs", {"hub_ids": ["dallas", "fort_worth"], "hazards": None, "season": None, "months": None})
    assert not err and len(res["hubs"][0]["hazards"]) == 6
    assert "selected_hazards_combined" not in res["comparison"]
    _, err = run_tool("compare_hubs", {"hub_ids": ["dallas"], "hazards": None, "season": None, "months": None})
    assert err  # at least 2 hubs


def test_eval_args_check_treats_lists_as_sets():
    from types import SimpleNamespace
    call = SimpleNamespace(name="compare_hubs", input={"hub_ids": ["houston", "miami"], "hazards": ["flood", "tropical"]})
    result = SimpleNamespace(tool_calls=[call])
    assert args_mismatch(result, {"hub_ids": ["miami", "houston"], "hazards": ["tropical", "flood"]}, "compare_hubs") is None
    assert args_mismatch(result, {"hub_ids": ["miami", "dallas"]}, "compare_hubs")


def test_eval_cases_file_parses():
    # Eval run 4 crashed on a YAML comment (" #17" inside a plain string); catch that offline.
    from pathlib import Path
    import yaml
    from evals.run import check_turn  # noqa: F401  (the runner imports cleanly)
    cases = yaml.safe_load((Path(__file__).parent.parent / "evals" / "cases.yaml").read_text(encoding="utf-8"))["cases"]
    assert len(cases) == 15 and all(c["turns"] and "why" in c for c in cases)
