"""Run the agent eval set against the real Claude API.

    uv run python -m evals.run                    # all cases
    uv run python -m evals.run --case out_of_scope

Prints a pass/fail table and writes evals/report.json. Exit code 1 if any check fails.
Cost: roughly $0.03 per turn with claude-sonnet-5-5 (9 turns ~ $0.30).
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.agent import trajectory
from app.agent.loop import AgentError, ask
from app.scoring import service
from app.settings import settings
from evals.checks import args_mismatch, ranking_mismatch, ungrounded_numbers, wrong_count_claims

HERE = Path(__file__).parent
PRICE_IN, PRICE_OUT = 2.0, 10.0  # USD per million tokens, claude-sonnet-5-5 list price (estimate only)


def check_turn(result, expect: dict, user_texts: list[str]) -> list[tuple[str, str | None]]:
    """Returns [(check name, failure message or None)]."""
    out = []
    a = result.answer
    called = [c.name for c in result.tool_calls]

    tool = expect.get("tool")
    if tool == "none":
        out.append(("no tool call", f"called {called}" if called else None))
    elif tool:
        out.append((f"calls {tool}", None if tool in called else f"called {called or 'nothing'}"))
    if "args" in expect:
        out.append(("tool arguments", args_mismatch(result, expect["args"], tool if tool and tool != "none" else "rank_hubs")))
    if "ranking" in expect:
        want_null = expect["ranking"] is None or expect["ranking"] == "null"
        ok = (a.ranking is None) == want_null
        out.append((f"ranking {'null' if want_null else 'present'}", None if ok else f"ranking is {a.ranking and 'present'}"))
    if "confidence" in expect:
        out.append(("confidence", None if a.confidence in expect["confidence"] else f"got {a.confidence}"))
    for s in expect.get("mentions", []):
        out.append((f"mentions '{s}'", None if s.lower() in a.answer_markdown.lower() else "not in answer"))

    # Applied to every turn
    out.append(("ranking = engine", ranking_mismatch(result)))
    bad = ungrounded_numbers(result, user_texts)
    out.append(("numbers grounded", f"not in tool results: {bad}" if bad else None))
    wrong = wrong_count_claims(result)
    out.append(("counts correct", f"wrong counts: {wrong}" if wrong else None))
    return out


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--case", help="run only this case id")
    args = p.parse_args(argv)

    cases = yaml.safe_load((HERE / "cases.yaml").read_text(encoding="utf-8"))["cases"]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            sys.exit(f"unknown case '{args.case}'")
    service.load_results(restore=True)

    report, n_checks, n_failed, tok_in, tok_out = [], 0, 0, 0, 0
    print(f"model: {settings.llm_model} (effort {settings.llm_effort})\n")
    for case in cases:
        history, user_texts = [], []
        print(f"=== {case['id']}: {' '.join(case['why'].split())}")
        for i, turn in enumerate(case["turns"], 1):
            q = turn["question"]
            user_texts.append(q)
            t0 = time.perf_counter()
            entry = {"case": case["id"], "turn": i, "question": q}
            try:
                result = ask(q, history)
            except AgentError as exc:
                print(f"  turn {i}: FAIL  agent error: {exc}")
                entry["error"] = str(exc)
                report.append(entry)
                n_checks += 1
                n_failed += 1
                break
            secs = time.perf_counter() - t0
            tok_in += result.input_tokens
            tok_out += result.output_tokens
            checks = check_turn(result, turn.get("expect", {}), user_texts)
            n_checks += len(checks)
            n_failed += sum(1 for _, err in checks if err)
            print(f"  turn {i}: \"{q}\"  ({secs:.1f}s, {result.steps} model calls, "
                  f"{result.input_tokens}+{result.output_tokens} tokens)")
            for c in result.tool_calls:
                print(f"      tool @ model call {c.step}: {c.name}({json.dumps(c.input)})"
                      f"{'  [CACHED duplicate]' if c.cached else ''}{'  [ERROR]' if c.is_error else ''}")
            if not result.tool_calls:
                print("      tools: none")
            for name, err in checks:
                print(f"    {'PASS' if not err else 'FAIL'}  {name}{'' if not err else '  -> ' + err}")
            entry.update(seconds=round(secs, 1), checks={n: e for n, e in checks}, answer=result.answer.model_dump(),
                         steps=result.steps, tokens=[result.input_tokens, result.output_tokens],
                         tool_calls=[{"name": c.name, "input": c.input, "step": c.step, "cached": c.cached,
                                      "is_error": c.is_error} for c in result.tool_calls])
            # One readable trajectory per turn: print with `uv run python -m app.agent.trajectory <file>`.
            trajectory.save(q, case["id"], result, HERE / "trajectories", f"{case['id']}_turn{i}")
            report.append(entry)
            history += [{"role": "user", "content": q}, {"role": "assistant", "content": result.answer.model_dump_json()}]
        print()

    dupes = sum(1 for e in report for c in e.get("tool_calls", []) if c["cached"])
    print(f"duplicate tool calls (served from cache): {dupes}")
    cost = tok_in / 1e6 * PRICE_IN + tok_out / 1e6 * PRICE_OUT
    print(f"RESULT: {n_checks - n_failed}/{n_checks} checks passed; "
          f"{tok_in}+{tok_out} tokens, ~${cost:.2f} (list-price estimate)")
    (HERE / "report.json").write_text(json.dumps({
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": settings.llm_model,
        "checks": n_checks, "failed": n_failed, "tokens": [tok_in, tok_out], "turns": report,
    }, indent=2, default=str), encoding="utf-8")
    print(f"report: {HERE / 'report.json'}")
    print(f"trajectories: {HERE / 'trajectories'} (view: uv run python -m app.agent.trajectory <file>)")
    sys.exit(1 if n_failed else 0)


if __name__ == "__main__":
    main()
