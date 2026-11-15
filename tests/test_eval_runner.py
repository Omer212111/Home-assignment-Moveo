"""The eval runner's summary and run comparison (pure functions, no API calls)."""
from evals.run import CORRECTNESS, EFFICIENCY, compare, default_max_calls, summarize


def entry(case, turn, checks, seconds=5.0, cost=0.03, steps=2):
    return {"case": case, "turn": turn, "seconds": seconds, "cost": cost, "steps": steps,
            "checks": {n: (cat, err) for n, cat, err in checks}}


def test_summary_separates_correctness_from_efficiency_and_gives_pass_rates():
    runs = [entry("a", 1, [("calls x", CORRECTNESS, None), ("model calls <= 2", EFFICIENCY, "3 model calls")], steps=3),
            entry("a", 1, [("calls x", CORRECTNESS, None), ("model calls <= 2", EFFICIENCY, None)]),
            entry("a", 1, [("calls x", CORRECTNESS, "called y"), ("model calls <= 2", EFFICIENCY, None)]),
            {"case": "b", "turn": 1, "error": "No final answer"}]
    s = summarize(runs)
    assert s["correctness"] == {"passed": 2, "total": 4}          # an agent error counts as a failure
    assert s["efficiency"] == {"passed": 2, "total": 3}
    assert s["pass_rates"]["a/turn1/calls x"] == round(2 / 3, 3)
    assert s["pass_rates"]["b/turn1/agent error"] == 0
    assert s["model_calls_mean"] == round(7 / 3, 2) and s["cost_total"] == 0.09


def test_compare_reports_regressions_improvements_and_deltas():
    prev = {"pass_rates": {"a/t1/x": 1.0, "a/t1/y": 0.5, "a/t1/z": 1.0}, "cost_per_turn": 0.03,
            "latency_p50": 8.0, "model_calls_mean": 2.6}
    curr = {"pass_rates": {"a/t1/x": 0.67, "a/t1/y": 1.0, "a/t1/z": 1.0, "b/t1/new": 1.0}, "cost_per_turn": 0.025,
            "latency_p50": 7.0, "model_calls_mean": 2.2}
    d = compare(prev, curr)
    assert d["worse"] == [("a/t1/x", 1.0, 0.67)] and d["better"] == [("a/t1/y", 0.5, 1.0)]
    assert d["new_checks"] == ["b/t1/new"] and d["cost_per_turn_change"] == -0.005
    assert d["latency_p50_change"] == -1.0


def test_default_model_call_budget():
    assert default_max_calls({"tool": "none"}) == 1
    assert default_max_calls({"tool": "rank_hubs"}) == 2
    assert default_max_calls({}) == 3


def test_run8_false_failures_now_pass():
    """Eval run 8: top_n=50 then 10, server showed 50. Both checks must now pass."""
    from app.agent.loop import AgentResult, ToolCall
    from app.agent.schemas import AgentAnswer, RankedHub, RankHubsInput
    from app.agent.tools import rank_hubs
    from app.scoring import service
    from evals.checks import args_mismatch, ranking_mismatch
    service.load_results(restore=True)
    base = {"hazard": "total", "region": None, "order": "most", "season": None, "months": None}
    calls = [ToolCall(name="rank_hubs", input={**base, "top_n": n}, is_error=False, duration_ms=0,
                      result=rank_hubs(RankHubsInput.model_validate({**base, "top_n": n}))) for n in (50, 10)]
    rows = calls[0].result["ranking"]                   # what the server attached: the 50-row list
    answer = AgentAnswer(answer_markdown="x", assumptions=[], confidence="medium", uncertainty_notes="",
                         data_sources=[], ranking=[RankedHub(**{k: r[k] for k in RankedHub.model_fields}) for r in rows])
    res = AgentResult(answer=answer, tool_calls=calls, model="m", steps=3, input_tokens=0, output_tokens=0)
    expected = {"hazard": "total", "top_n": 50}
    assert args_mismatch(res, expected) is None
    assert ranking_mismatch(res, expected) is None
    assert ranking_mismatch(res, {"hazard": "total", "top_n": 10}) is not None   # the 10-row list was not shown
