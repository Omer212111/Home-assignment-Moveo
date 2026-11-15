"""Offline tests for the API, the tools and the agent loop (Claude is replaced by a scripted fake)."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agent import loop
from app.agent.schemas import AgentAnswer
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
    schema = AgentAnswer.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_run_tool_validates_arguments(api):
    result, is_error = run_tool("rank_hubs", {"hazard": "volcano", "region": None, "top_n": 5})
    assert is_error and result["error"] == "invalid arguments"
    result, is_error = run_tool("rank_hubs", {"hazard": "total", "region": None, "top_n": 3})
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
    a = {"answer_markdown": "Top hubs ...", "ranking": None, "assumptions": ["rules are expert judgment"],
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
        _msg("tool_use", _tool_use("rank_hubs", {"hazard": "total", "region": None, "top_n": 3})),
        _msg("end_turn", _text(_answer())),
    ])
    monkeypatch.setattr(loop, "client", lambda: fake)
    res = loop.ask("Which hubs are most dangerous?")
    assert res.answer.confidence == "medium" and res.steps == 2
    assert [c.name for c in res.tool_calls] == ["rank_hubs"] and not res.tool_calls[0].is_error
    sent = fake.requests[1]["messages"][-1]["content"][0]   # tool result sent back to the model
    assert sent["type"] == "tool_result" and json.loads(sent["content"])["ranking"][0]["rank"] == 1
    assert fake.requests[0]["output_format"] is AgentAnswer


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
