"""Offline tests for the API, the tools and the agent loop (Claude is replaced by a scripted fake)."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agent import loop
from app.agent.schemas import AgentAnswer, ModelAnswer
from app.agent.tools import run_tool, tool_definitions
from app.main import app


@pytest.fixture(scope="module")
def api():
    with TestClient(app) as c:   # runs startup: snapshot -> SQLite -> scores
        yield c


# ---------------------------------------------------------------- deterministic endpoints

def test_health(api):
    body = api.get("/health").json()
    assert body["hubs"] == 50 and body["hubs_complete"] == 50


def test_rankings_sorted_and_filtered(api):
    body = api.get("/rankings", params={"hazard": "winter", "region": "Midwest", "top_n": 5}).json()
    days = [r["days_per_year"] for r in body["ranking"]]
    assert len(days) == 5 and days == sorted(days, reverse=True)
    assert all(r["region"] == "Midwest" for r in body["ranking"])


def test_rankings_rejects_unknown_hazard(api):
    assert api.get("/rankings", params={"hazard": "volcano"}).status_code == 422


def test_hub_detail_and_404(api):
    assert api.get("/hubs/dallas").json()["city"] == "Dallas"
    assert api.get("/hubs/paris").status_code == 404


# ---------------------------------------------------------------- tools

def test_tool_schemas_are_strict():
    for t in tool_definitions():
        assert t["strict"] is True
        schema = t["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])  # strict mode: all required


def test_answer_schema_is_strict():
    schema = ModelAnswer.model_json_schema()
    assert "ranking" not in schema["properties"]   # the model never re-types the ranking
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_run_tool_validates_arguments(api):
    result, is_error = run_tool("rank_hubs", {"hazard": "volcano", "region": None, "top_n": 5, "order": "most", "season": None, "months": None})
    assert is_error and result["error"] == "invalid arguments"
    result, is_error = run_tool("rank_hubs", {"hazard": "total", "region": None, "top_n": 3, "order": "most", "season": None, "months": None})
    assert not is_error and [r["rank"] for r in result["ranking"]] == [1, 2, 3]
    assert "method" in result


# ---------------------------------------------------------------- agent loop with a fake Claude

def _msg(stop_reason, *blocks):
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks), model="fake-model",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def _tool_use(name, inp, id_="t1"):
    return SimpleNamespace(type="tool_use", name=name, input=inp, id=id_)


def _text(s):
    return SimpleNamespace(type="text", text=s)


def _answer(**overrides):
    a = {"answer_markdown": "Top hubs ...", "assumptions": ["rules are expert judgment"],
         "confidence": "medium", "uncertainty_notes": "not calibrated", "data_sources": ["Open-Meteo 2021-2025"]}
    return json.dumps({**a, **overrides})


class FakeClient:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def test_loop_runs_tool_then_returns_validated_answer(api, monkeypatch):
    fake = FakeClient([
        _msg("tool_use", _tool_use("rank_hubs", {"hazard": "total", "region": None, "top_n": 3, "order": "most", "season": None, "months": None})),
        _msg("end_turn", _text(_answer())),
    ])
    monkeypatch.setattr(loop, "client", lambda: fake)
    res = loop.ask("Which hubs are most dangerous?")
    assert res.answer.confidence == "medium" and res.steps == 2
    assert [c.name for c in res.tool_calls] == ["rank_hubs"] and not res.tool_calls[0].is_error
    sent = fake.requests[1]["messages"][-1]["content"][0]   # tool result sent back to the model
    assert sent["type"] == "tool_result" and json.loads(sent["content"])["ranking"][0]["rank"] == 1
    assert fake.requests[0]["output_format"] is ModelAnswer
    # the ranking is attached by the server, straight from the engine's result
    assert [r.rank for r in res.answer.ranking] == [1, 2, 3]
    assert res.answer.ranking[0].hub_id == res.tool_calls[0].result["ranking"][0]["hub_id"]


def test_loop_retries_once_on_schema_mismatch(api, monkeypatch):
    fake = FakeClient([_msg("end_turn", _text('{"answer_markdown": "missing fields"}')),
                       _msg("end_turn", _text(_answer()))])
    monkeypatch.setattr(loop, "client", lambda: fake)
    assert loop.ask("hi").steps == 2


def test_loop_reports_refusal(api, monkeypatch):
    monkeypatch.setattr(loop, "client", lambda: FakeClient([_msg("refusal")]))
    with pytest.raises(loop.AgentError, match="declined"):
        loop.ask("hi")


def test_chat_endpoint_keeps_history(api, monkeypatch):
    fake = FakeClient([_msg("end_turn", _text(_answer())), _msg("end_turn", _text(_answer()))])
    monkeypatch.setattr(loop, "client", lambda: fake)
    first = api.post("/chat", json={"message": "first question"}).json()
    api.post("/chat", json={"message": "follow-up", "session_id": first["session_id"]})
    msgs = fake.requests[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"] == "first question" and msgs[-1]["content"] == "follow-up"


# ---------------------------------------------------------------- real SDK against a fake network

def _sdk_client(responses, captured):
    """A real anthropic client whose HTTP layer returns canned API responses."""
    import anthropic
    import httpx2

    def handler(request):
        captured.append(json.loads(request.content))
        status, body = responses.pop(0)
        return httpx2.Response(status, json=body)
    return anthropic.Anthropic(api_key="test", max_retries=0,
                               http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def _api_message(stop_reason, content):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20}}


def test_tool_schema_has_no_unsupported_constraints():
    schema = json.dumps(tool_definitions()[0]["input_schema"])
    assert '"minimum"' not in schema and '"maximum"' not in schema


def test_real_sdk_round_trip(api, monkeypatch):
    captured = []
    fake = _sdk_client([
        (200, _api_message("tool_use", [{"type": "tool_use", "id": "tu_1", "name": "rank_hubs",
                                         "input": {"hazard": "total", "region": None, "top_n": 3, "order": "most", "season": None, "months": None}}])),
        (200, _api_message("end_turn", [{"type": "text", "text": _answer()}])),
    ], captured)
    monkeypatch.setattr(loop, "client", lambda: fake)
    res = loop.ask("Which hubs are most dangerous?")
    assert res.answer.confidence == "medium" and [c.name for c in res.tool_calls] == ["rank_hubs"]
    first, second = captured
    assert first["output_config"]["effort"] == "low" and first["output_config"]["format"]["type"] == "json_schema"
    assert first["tools"][0]["strict"] is True and first["fallbacks"] == "default"
    assert second["messages"][-1]["content"][0]["type"] == "tool_result"


def test_api_error_becomes_readable_agent_error(api, monkeypatch):
    fake = _sdk_client([(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                          "message": "bad schema"}})], [])
    monkeypatch.setattr(loop, "client", lambda: fake)
    with pytest.raises(loop.AgentError, match="400.*bad schema"):
        loop.ask("hi")


def test_chat_returns_502_with_message_on_api_error(api, monkeypatch):
    fake = _sdk_client([(401, {"type": "error", "error": {"type": "authentication_error",
                                                          "message": "invalid x-api-key"}})], [])
    monkeypatch.setattr(loop, "client", lambda: fake)
    r = api.post("/chat", json={"message": "hi"})
    assert r.status_code == 502 and "rejected the key" in r.json()["detail"]


def test_index_serves_chat_ui(api):
    r = api.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert 'fetch("/chat"' in r.text



def test_identical_tool_calls_are_served_from_cache(api, monkeypatch):
    args = {"hazard": "total", "region": None, "top_n": 10, "order": "most", "season": None, "months": None}
    fake = FakeClient([
        _msg("tool_use", _tool_use("rank_hubs", args, "a"), _tool_use("rank_hubs", dict(args), "b")),
        _msg("end_turn", _text(_answer())),
    ])
    monkeypatch.setattr(loop, "client", lambda: fake)
    res = loop.ask("top 10?")
    assert [c.cached for c in res.tool_calls] == [False, True]
    assert [c.step for c in res.tool_calls] == [1, 1]          # both requested in the same response
    sent = fake.requests[1]["messages"][-1]["content"]
    assert len(sent) == 2 and "already answered" in sent[1]["content"]   # every tool_use gets a result


def test_no_tool_call_means_no_ranking(api, monkeypatch):
    monkeypatch.setattr(loop, "client", lambda: FakeClient([_msg("end_turn", _text(_answer(confidence="low")))]))
    assert loop.ask("weather in Paris?").answer.ranking is None


def test_trajectory_records_thinking_tools_and_answer(api, monkeypatch):
    from app.agent import trajectory
    thinking = SimpleNamespace(type="thinking", thinking="The user asks for the riskiest hubs; use rank_hubs.")
    fake = FakeClient([
        _msg("tool_use", thinking, _tool_use("rank_hubs", {"hazard": "total", "region": None, "top_n": 3, "order": "most", "season": None, "months": None})),
        _msg("end_turn", _text(_answer())),
    ])
    monkeypatch.setattr(loop, "client", lambda: fake)
    res = loop.ask("Which hubs are most dangerous?")
    assert fake.requests[0]["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert [e["type"] for e in res.trajectory] == ["model_call", "tool_result", "model_call", "final_answer"]
    first = res.trajectory[0]["blocks"]
    assert first[0] == {"type": "thinking", "text": "The user asks for the riskiest hubs; use rank_hubs."}
    assert first[1]["type"] == "tool_use" and first[1]["name"] == "rank_hubs"
    assert res.trajectory[1]["summary"].startswith("3 hubs ranked")
    # saved file renders as a readable timeline
    path = trajectory.save("Which hubs are most dangerous?", "abc12345", res)
    text = trajectory.render(json.loads(path.read_text(encoding="utf-8")))
    assert "thinks: The user asks" in text and "calls:  rank_hubs" in text and "[answer] confidence medium" in text


def test_chat_returns_and_saves_trajectory(api, monkeypatch):
    from app.settings import settings
    fake = FakeClient([_msg("end_turn", _text(_answer()))])
    monkeypatch.setattr(loop, "client", lambda: fake)
    body = api.post("/chat", json={"message": "hello"}).json()
    assert body["result"]["trajectory"][-1]["type"] == "final_answer"
    assert len(list(settings.trajectory_dir.glob("*.json"))) == 1


def test_repeated_calls_force_an_answer_and_failures_keep_their_trajectory(api, monkeypatch):
    """Eval run 4: the model repeated identical calls until MAX_STEPS. After a round of only
    repeated calls the next call disables tools (tool_choice none), so the model must answer."""
    args = {"hazard": "winter", "region": "Midwest", "top_n": 3, "order": "most", "season": None, "months": None}
    fake = FakeClient([
        _msg("tool_use", _tool_use("rank_hubs", args, "a")),
        _msg("tool_use", _tool_use("rank_hubs", dict(args), "b")),   # repeat -> served from cache
        _msg("end_turn", _text(_answer())),
    ])
    monkeypatch.setattr(loop, "client", lambda: fake)
    res = loop.ask("Which 3 Midwest hubs are most exposed to winter disruption?")
    assert res.steps == 3
    assert "tool_choice" not in fake.requests[0] and "tool_choice" not in fake.requests[1]
    assert fake.requests[2]["tool_choice"] == {"type": "none"}
    assert res.trajectory[-2]["tools_disabled"] == "only repeated calls before"

    # Different calls each round are allowed until the last call, which must answer.
    fake = FakeClient([_msg("tool_use", _tool_use("rank_hubs", {**args, "top_n": n}, f"t{n}"))
                       for n in range(1, loop.MAX_STEPS)] + [_msg("end_turn", _text("not json"))])
    monkeypatch.setattr(loop, "client", lambda: fake)
    with pytest.raises(loop.AgentError) as err:
        loop.ask("x")
    assert fake.requests[-1]["tool_choice"] == {"type": "none"}
    assert [e["type"] for e in err.value.trajectory].count("model_call") == loop.MAX_STEPS

    from app.agent import trajectory
    text = trajectory.render(json.loads(trajectory.save("x", "s", None, error=err.value).read_text(encoding="utf-8")))
    assert "FAILED:" in text and "tools disabled: last allowed call" in text
