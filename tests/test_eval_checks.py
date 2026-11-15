"""The eval checks themselves are tested offline, including on the real answer that said
'8 of the 10' when the tool's ranking had 9 winter hubs."""
import pytest

from app.agent.loop import AgentResult, ToolCall
from app.agent.schemas import AgentAnswer, RankedHub, RankHubsInput
from app.agent.tools import rank_hubs
from app.scoring import service
from evals.checks import args_mismatch, ranking_mismatch, ungrounded_numbers, wrong_count_claims


@pytest.fixture(scope="module", autouse=True)
def scores():
    service.load_results(restore=True)


def make_result(text, ranking_rows=None, args=None, notes="ok"):
    args = args or {"hazard": "total", "region": None, "top_n": 10}
    tool_result = rank_hubs(RankHubsInput.model_validate(args))
    rows = tool_result["ranking"] if ranking_rows is None else ranking_rows
    answer = AgentAnswer(answer_markdown=text, assumptions=[], confidence="medium", uncertainty_notes=notes,
                         data_sources=[], ranking=[RankedHub(rank=r["rank"], hub_id=r["hub_id"], city=r["city"],
                                                             days_per_year=r["days_per_year"], main_hazard=r["main_hazard"])
                                                   for r in rows])
    return AgentResult(answer=answer, model="m", steps=2, input_tokens=0, output_tokens=0,
                       tool_calls=[ToolCall(name="rank_hubs", input=args, is_error=False, duration_ms=0,
                                            result=tool_result)])


def test_real_wrong_count_is_caught():
    r = make_result("Winter storms, snow and ice drive 8 of the 10 most exposed hubs.")
    assert wrong_count_claims(r) == ["8 of 10"]
    assert wrong_count_claims(make_result("Winter drives 9 of the 10 most exposed hubs.")) == []


def test_grounding_allows_rounding_and_flags_invented_numbers():
    assert ungrounded_numbers(make_result("Boston leads with 13.07 days/yr, Minneapolis 11.7."), []) == []
    assert ungrounded_numbers(make_result("Boston loses about 14.2 days and $987 million a year."), []) == ["14.2", "987"]


def test_numbers_from_the_question_are_allowed():
    assert ungrounded_numbers(make_result("You asked for the top 100; only 50 hubs exist."), ["top 100 hubs"]) == []


def test_ranking_must_equal_engine():
    r = make_result("x")
    assert ranking_mismatch(r) is None
    swapped = list(r.tool_calls[0].result["ranking"])
    swapped[0], swapped[1] = swapped[1], swapped[0]
    assert "!=" in ranking_mismatch(make_result("x", ranking_rows=swapped))


def test_args_mismatch():
    r = make_result("x", args={"hazard": "winter", "region": "Midwest", "top_n": 3})
    assert args_mismatch(r, {"hazard": "winter", "region": "Midwest", "top_n": 3}) is None
    assert "top_n" in args_mismatch(r, {"top_n": 5})
