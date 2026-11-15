"""Unit tests for the disruption-days engine, on small hand-checkable synthetic data."""
from datetime import date, timedelta

import pandas as pd
import pytest

from app.config import load_scoring
from app.scoring.disruption import classify_days, expand_events, hub_disruption, rank

CFG = load_scoring()
HUB = {"hub_id": "test", "city": "Test", "state": "TX", "region": "South", "county_fips": "48113"}
START = date(2025, 1, 1)


def calm_year(n=365):
    """n calm days: nothing triggers."""
    return pd.DataFrame({
        "hub_id": "test", "date": [str(START + timedelta(days=i)) for i in range(n)],
        "tmax": 20.0, "tmin": 10.0, "precip_sum": 0.0, "rain_sum": 0.0, "snowfall_sum": 0.0,
        "wind_gusts_max": 30.0, "wind_speed_max": 10.0, "weather_code": 3,
    })


def set_day(df, day_index, **values):
    for k, v in values.items():
        df.loc[day_index, k] = v
    return df


def no_decl():
    return pd.DataFrame(columns=["disaster_number", "county_fips", "incident_type", "declaration_type",
                                 "title", "declaration_date", "incident_begin_date"])


def run(df, decl=None, n=365):
    return hub_disruption(HUB, df, decl if decl is not None else no_decl(), CFG,
                          (START, START + timedelta(days=n - 1)))


def test_calm_year_is_zero():
    r = run(calm_year())
    assert r.total_days_per_year == 0 and r.weather_complete


def test_worst_rule_wins_and_day_counted_once():
    # snow 12 cm (1.0) + gusts 80 km/h (0.5) on the same day -> one day, winter, severity 1.0
    df = set_day(calm_year(), 10, snowfall_sum=12.0, wind_gusts_max=80.0)
    t = classify_days(df, CFG)
    assert len(t) == 1 and t.iloc[0]["hazard"] == "winter" and t.iloc[0]["severity"] == 1.0


def test_storm_of_three_days_gets_one_recovery_tail():
    # 3 consecutive days of 12 cm snow = one event: 3 closed days + 1 recovery day
    df = calm_year()
    for i in (10, 11, 12):
        set_day(df, i, snowfall_sum=12.0)
    r = run(df)
    assert r.hazards["winter"].from_weather == pytest.approx(4 / (365 / 365.25), abs=0.01)
    assert r.hazards["winter"].events_per_year == pytest.approx(1.0, abs=0.01)


def test_longest_recovery_in_event_is_used():
    # day 1: 12 cm (tail 1), day 2: 30 cm (tail 2) -> 2 trigger days + 2 recovery days
    df = set_day(set_day(calm_year(), 10, snowfall_sum=12.0), 11, snowfall_sum=30.0)
    days = expand_events(classify_days(df, CFG), CFG, last_date=START + timedelta(days=364))
    assert (days["kind"] == "trigger").sum() == 2 and (days["kind"] == "recovery").sum() == 2


def test_recovery_tail_is_cut_by_next_event():
    # 30 cm snow (tail 2) on day 10, then a new event on day 12 -> only day 11 is recovery
    df = set_day(set_day(calm_year(), 10, snowfall_sum=30.0), 12, snowfall_sum=12.0)
    days = expand_events(classify_days(df, CFG), CFG, last_date=START + timedelta(days=364))
    recovery = days[days["kind"] == "recovery"]["date"].tolist()
    assert START + timedelta(days=11) in recovery
    assert START + timedelta(days=12) not in recovery  # that is a trigger day of the next event
    assert len(days) == len(set(days["date"]))           # no date counted twice


def test_partial_severity_days():
    # light snow (0.3) and extreme heat (0.15)
    df = set_day(set_day(calm_year(), 5, snowfall_sum=3.0), 200, tmax=40.0)
    r = run(df)
    assert r.hazards["winter"].from_weather == pytest.approx(0.3 * 365.25 / 365, abs=0.01)
    assert r.hazards["heat"].from_weather == pytest.approx(0.15 * 365.25 / 365, abs=0.01)


def test_missing_values_never_trigger():
    df = set_day(calm_year(), 5, snowfall_sum=None, tmax=None, wind_gusts_max=None)
    assert run(df).total_days_per_year == 0


def decl_row(num, itype, dtype, begin):
    return {"disaster_number": num, "county_fips": "48113", "incident_type": itype, "declaration_type": dtype,
            "title": f"{itype} {num}", "declaration_date": begin, "incident_begin_date": begin}


def test_declarations_annualised_dr_only():
    decl = pd.DataFrame([decl_row(1, "Hurricane", "DR", "2010-08-25"),
                         decl_row(2, "Hurricane", "EM", "2012-08-25"),      # EM: not counted
                         decl_row(3, "Tornado", "DR", "2015-05-01"),
                         decl_row(4, "Snowstorm", "DR", "2016-02-01")])    # winter: weather channel only
    r = run(calm_year(), decl)
    assert r.hazards["tropical"].from_declarations == pytest.approx(5.0 / 20)
    assert r.hazards["severe_storm"].from_declarations == pytest.approx(1.5 / 20)
    assert r.hazards["winter"].from_declarations == 0
    assert {c.disaster_number for c in r.declarations_counted} == {1, 3}


def test_weather_days_inside_declaration_window_are_not_double_counted():
    # 150 mm of rain on the day a counted hurricane starts -> attributed to the hurricane
    df = set_day(calm_year(), 100, precip_sum=150.0)
    hurricane_day = str(START + timedelta(days=100))
    r = run(df, pd.DataFrame([decl_row(1, "Hurricane", "DR", hurricane_day)]))
    assert r.hazards["flood"].from_weather == 0
    assert r.weather_days_deduplicated == 3          # 1 trigger day + 2 recovery days
    assert r.hazards["tropical"].from_declarations == pytest.approx(0.25)


def test_incomplete_weather_is_flagged_and_not_ranked():
    full = run(set_day(calm_year(), 5, snowfall_sum=12.0))
    partial = run(calm_year(100))  # window still says 365 days -> coverage ~27%
    partial = partial.model_copy(update={"hub_id": "partial"})
    assert not partial.weather_complete
    out = rank([full, partial])
    assert [r["hub_id"] for r in out["ranking"]] == ["test"]
    assert out["excluded_incomplete_data"] == ["partial"]


def test_multi_condition_rule_needs_all_conditions():
    from app.config import WeatherRule
    rule = WeatherRule(hazard="winter", severity=1.0, recovery_days=1, label="ice",
                       conditions=[{"variable": "rain_sum", "op": ">=", "threshold": 1},
                                   {"variable": "tmax", "op": "<=", "threshold": 1}])
    from app.scoring.disruption import _rule_mask
    df = calm_year(3)
    set_day(df, 0, rain_sum=5.0, tmax=0.0)   # rain + freezing -> ice
    set_day(df, 1, rain_sum=5.0)             # rain, warm -> no
    set_day(df, 2, tmax=-5.0)                # cold, dry -> no
    assert _rule_mask(df, rule).tolist() == [True, False, False]
