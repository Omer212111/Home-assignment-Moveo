"""Agent trajectories: what happened inside one question, step by step.

A trajectory is a list of events, in order:
  model_call   one Messages API call: the model's thinking summary, any text, the tools it asked for
  tool_result  one tool run: arguments, a one-line summary of the result, cache hit, error, time
  schema_retry the answer did not match the schema and the model was asked again
  final_answer the structured answer was accepted

Each /chat question is saved as data/trajectories/<time>_<session>.json (git-ignored), and the
eval runner saves one per turn under evals/trajectories/. Print one as a readable timeline:

    uv run python -m app.agent.trajectory              # the latest file
    uv run python -m app.agent.trajectory <file.json>
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.settings import settings


def model_call_event(step: int, response, duration_ms: int) -> dict:
    blocks = []
    for b in response.content:
        t = getattr(b, "type", "unknown")
        if t == "thinking":
            # Empty unless the request asks for display "summarized" (the API default is "omitted").
            blocks.append({"type": "thinking", "text": b.thinking or "(thinking not displayed)"})
        elif t == "redacted_thinking":
            blocks.append({"type": "thinking", "text": "(redacted)"})
        elif t == "text":
            blocks.append({"type": "text", "text": b.text})
        elif t == "tool_use":
            blocks.append({"type": "tool_use", "name": b.name, "input": b.input})
        else:  # e.g. a refusal "fallback" block: keep that it happened
            blocks.append({"type": t})
    u = response.usage
    reads = getattr(u, "cache_read_input_tokens", 0) or 0
    writes = getattr(u, "cache_creation_input_tokens", 0) or 0
    return {"type": "model_call", "step": step, "model": response.model, "stop_reason": response.stop_reason,
            "duration_ms": duration_ms, "input_tokens": u.input_tokens + reads + writes,
            "cache_read_tokens": reads, "output_tokens": u.output_tokens, "blocks": blocks}


def result_summary(name: str, result: dict, is_error: bool) -> str:
    """One line describing a tool result, for people reading the trajectory."""
    if is_error:
        return f"error: {result.get('error')} {json.dumps(result.get('details', ''))[:200]}"
    if name == "rank_hubs":
        rows = result.get("ranking", [])
        top = ", ".join(f"{r['city']} {r['days_per_year']:.2f}" for r in rows[:3])
        return (f"{len(rows)} hubs ranked ({result.get('hazard')}, region {result.get('region') or 'all'}, "
                f"{result.get('period', 'whole year')}, {result.get('order', 'most exposed first')}); top: {top}")
    if name == "explain_hub":
        c = result["context"]
        return (f"{result['hub']['city']}: {result['total_days_per_year']:.2f} days/yr, rank {c['overall_rank']} "
                f"of {c['hubs_ranked']} (median {c['median_hub_days_per_year']})")
    if name == "compare_hubs":
        return (f"compared ({result.get('period', 'whole year')}) "
                + ", ".join(f"{h['hub']['city']} {h['total_days_per_year']:.2f}" for h in result["hubs"]))
    if name == "hub_breakdown":
        return f"by {result['by']}: " + "; ".join(
            f"{h['hub']['city']} highest {h['highest_period']}, lowest {h['lowest_period']}" for h in result["hubs"])
    if name == "weather_stat":
        def one(h):
            if "percentage_of_days" in h:
                return f"{h['hub']['city']} {h['days_matching']}/{h['days_in_period']} days ({h['percentage_of_days']}%)"
            return f"{h['hub']['city']} {h.get('value')}"
        return f"{result['variable']}, {result['period']}: " + ", ".join(one(h) for h in result["hubs"])
    return f"{len(json.dumps(result))} characters"


def save(question: str, session_id: str, result=None, directory: Path | None = None, name: str | None = None,
         error: Exception | None = None) -> Path:
    """Save a trajectory: from an AgentResult, or from an AgentError (the steps up to the failure)."""
    directory = directory or settings.trajectory_dir
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    path = directory / f"{name or now.strftime('%Y%m%dT%H%M%S') + '_' + session_id[:8]}.json"
    events = result.trajectory if result is not None else getattr(error, "trajectory", [])
    calls = [e for e in events if e["type"] == "model_call"]
    path.write_text(json.dumps({
        "saved_at": now.isoformat(timespec="seconds"), "session_id": session_id, "question": question,
        "model": result.model if result is not None else (calls[-1]["model"] if calls else None),
        "steps": len(calls),
        "tokens": [sum(e["input_tokens"] for e in calls), sum(e["output_tokens"] for e in calls)],
        "trajectory": events,
        "answer": result.answer.model_dump() if result is not None else None,
        "error": str(error) if error is not None else None,
    }, indent=2, default=str), encoding="utf-8")
    return path


def render(data: dict) -> str:
    """A readable timeline of one saved trajectory."""
    out = [f"Q: {data['question']}", f"model {data['model']}, {data['steps']} model calls, "
           f"{data['tokens'][0]}+{data['tokens'][1]} tokens", ""]
    for e in data["trajectory"]:
        if e["type"] == "model_call":
            out.append(f"[model call {e['step']}] {e['duration_ms']} ms, {e['input_tokens']}+{e['output_tokens']} "
                       f"tokens ({e.get('cache_read_tokens', 0)} from cache), stop: {e['stop_reason']}"
                       + (f"  (tools disabled: {e['tools_disabled']})" if e.get("tools_disabled") else ""))
            for b in e["blocks"]:
                if b["type"] == "thinking":
                    out += ["   thinks: " + line for line in b["text"].splitlines() if line.strip()]
                elif b["type"] == "tool_use":
                    out.append(f"   calls:  {b['name']}({json.dumps(b['input'])})")
                elif b["type"] == "text" and e["stop_reason"] != "end_turn":
                    out.append(f"   says:   {b['text'][:300]}")
                elif b["type"] not in ("text",):
                    out.append(f"   [{b['type']}]")
        elif e["type"] == "tool_result":
            flag = " [cached]" if e["cached"] else " [ERROR]" if e["is_error"] else ""
            out.append(f"   -> {e['name']}{flag} ({e['duration_ms']} ms): {e['summary']}")
        elif e["type"] == "schema_retry":
            out.append(f"   !! answer did not match the schema, asked again: {e['error'][:200]}")
        elif e["type"] == "final_answer":
            out.append(f"[answer] confidence {e['confidence']}")
    if data.get("error"):
        out += ["", f"FAILED: {data['error']}"]
    if data.get("answer"):
        out += ["", data["answer"]["answer_markdown"]]
    return "\n".join(out)


def main(argv: list[str]) -> None:
    if argv:
        path = Path(argv[0])
    else:
        files = sorted(settings.trajectory_dir.glob("*.json"))
        if not files:
            sys.exit(f"no trajectories in {settings.trajectory_dir}; ask the chat a question first")
        path = files[-1]
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    print(f"{path}\n")
    print(render(json.loads(path.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    main(sys.argv[1:])
