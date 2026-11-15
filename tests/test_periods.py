"""Time breakdowns (month, season, year) and the weather_stat tool."""
import pandas as pd
import pytest

from app.agent.tools import run_tool
from app.scoring import service
from app.scoring.periods import SEASONS, breakdown, period_days

P = {"season": None, "months": None}
HAZARDS = ["total", "winter", "flood", "high_wind", "heat", "tropical", "severe_storm"]


@pytest.fixture(scope="module", autouse=True)
def scores():
    service.load_results(restore=True)


def test_periods_add_up_to_the_annual_total_for_every_hub_and_hazard():
    for r in service.results():
        for hz in HAZARDS:
            annual = r.total_days_per_year if hz == "total" else r.hazards[hz].days_per_year
            assert period_days(r, hz, None) == pytest.approx(annual, abs=0.01)
            assert sum(period_days(r, hz, m) for m in SEASONS.values()) == pytest.approx(annual, abs=0.01)
            months = breakdown(r, hz, "month", [])
            assert sum(x["days_per_year"] for x in months) == pytest.approx(annual, abs=0.02)


def test_year_breakdown_counts_actual_days_in_that_year():
    r = service.by_id("denver")
    years = breakdown(r, "total", "year", [2021, 2022, 2023, 2024, 2025])
    weather_2024 = sum(x.value for x in r.records if x.channel == "weather" and x.date.year == 2024)
    decl_2024 = sum(x.value for x in r.records if x.channel == "declarations" and x.date.year == 2024)
    assert next(y for y in years if y["period"] == 2024)["days"] == pytest.approx(weather_2024 + decl_2024, abs=0.01)


def test_hub_breakdown_denver_peaks_in_march_and_compares_hubs():
    res, err = run_tool("hub_breakdown", {"hub_ids": ["denver"], "hazard": "total", "by": "month"})
    assert not err and res["hubs"][0]["highest_period"] == "Mar"
    res, err = run_tool("hub_breakdown", {"hub_ids": ["boston", "minneapolis"], "hazard": "total", "by": "season"})
    assert not err and set(res["comparison_by_period"]) == {"winter", "spring", "summer", "autumn"}
    assert "seasons_definition" in res and "small_sample_warning" in res


def test_rank_hubs_by_season_and_safest_first():
    res, _ = run_tool("rank_hubs", {**P, "hazard": "total", "region": None, "top_n": 3, "order": "most",
                                    "season": "summer"})
    assert res["ranking"][0]["city"] == "Phoenix" and res["period"].startswith("summer")
    days = [r["days_per_year"] for r in res["ranking"]]
    assert days == sorted(days, reverse=True)
    res, _ = run_tool("rank_hubs", {**P, "hazard": "total", "region": None, "top_n": 5, "order": "least"})
    days = [r["days_per_year"] for r in res["ranking"]]
    assert days == sorted(days) and res["ranking"][0]["city"] == "San Diego"
    _, err = run_tool("rank_hubs", {**P, "hazard": "total", "region": None, "top_n": 3, "order": "most",
                                    "season": "winter", "months": [1]})
    assert err  # season and months together are rejected


def test_compare_hubs_in_hurricane_season_only_counts_those_months():
    res, err = run_tool("compare_hubs", {"hub_ids": ["miami", "houston"], "hazards": ["tropical"],
                                         "season": None, "months": [6, 7, 8, 9, 10, 11]})
    assert not err and res["period"].startswith("months: Jun")
    for h in res["hubs"]:
        for d in h["hazards"]["tropical"]["fema_disasters_counted"]:
            assert 6 <= int(d["incident_start"][5:7]) <= 11


def test_weather_stat_denver_snowfall_2025_matches_raw_data():
    res, err = run_tool("weather_stat", {**P, "hub_ids": ["denver"], "variable": "snowfall_cm",
                                         "statistic": "count_days", "condition_op": ">", "condition_value": 0,
                                         "year": 2025})
    assert not err
    w = service.weather("denver")
    w = w[w["date"].str.startswith("2025")]
    h = res["hubs"][0]
    assert h["days_in_period"] == len(w) == 365
    assert h["days_matching"] == int((w["snowfall_sum"] > 0).sum())
    assert h["days_with_at_least_1_cm"] == int((w["snowfall_sum"] >= 1).sum())
    assert h["percentage_of_days"] == round(100 * h["days_matching"] / 365, 1)
    assert h["date_range"] == "2025-01-01..2025-12-31"


def test_weather_stat_max_with_date_and_validation():
    res, err = run_tool("weather_stat", {**P, "hub_ids": ["phoenix", "dallas"], "variable": "max_temp_c",
                                         "statistic": "max", "condition_op": None, "condition_value": None,
                                         "year": 2024, "season": "summer"})
    assert not err and res["comparison"]["highest"] == "Phoenix" and res["hubs"][0]["on_date"].startswith("2024-0")
    _, err = run_tool("weather_stat", {**P, "hub_ids": ["denver"], "variable": "snowfall_cm",
                                       "statistic": "count_days", "condition_op": None, "condition_value": None,
                                       "year": 2025})
    assert err  # count_days needs a condition
    _, err = run_tool("weather_stat", {**P, "hub_ids": ["denver"], "variable": "snowfall_cm", "statistic": "sum",
                                       "condition_op": None, "condition_value": None, "year": 2019})
    assert err  # outside the data window


def test_rankings_endpoint_supports_season_and_order():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        body = c.get("/rankings", params={"season": "summer", "top_n": 1}).json()
        assert body["ranking"][0]["hub_id"] == "phoenix"
        body = c.get("/rankings", params={"order": "least", "top_n": 1}).json()
        assert body["ranking"][0]["hub_id"] == "san_diego"
