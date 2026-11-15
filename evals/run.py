"""Run the agent eval set against the real Claude API.

    uv run python -m evals.run                                   # all cases, once
    uv run python -m evals.run --case out_of_scope,safest_hubs   # some cases
    uv run python -m evals.run --repeat 3                        # each case 3 times: pass rates

Two kinds of checks per turn:
- correctness: right tool and arguments, ranking = engine, numbers grounded, counts, expected and
  forbidden phrases, confidence. These decide whether the answer is right.
- efficiency: model calls and latency within the case's budget. Reported as a separate score, so
  "correct but slow" is visible without hiding real errors.

Writes evals/report.json (this run) and evals/history/<time>_<commit>.json, and prints what changed
compared with the previous run in evals/history/. Exit code 1 if a correctness check fails.
Cost: roughly $0.03 per turn with claude-sonnet-5-5 (26 turns ~ $0.80 per repeat).
"""
import argparse
import json
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.agent import trajectory
from app.agent.loop import AgentError, ask
from app.scoring import service
from app.settings import settings
from evals.checks import (args_mismatch, mentions_forbidden, ranking_mismatch, ungrounded_numbers,
                          wrong_count_claims)

HERE = Path(__file__).parent
HISTORY = HERE / "history"
# USD per million tokens, claude-sonnet-5-5 list price (estimate only): uncached input, cache write
# (1.25x, 5-minute cache), cache read, output.
PRICE_IN, PRICE_CACHE_WRITE, PRICE_CACHE_READ, PRICE_OUT = 2.0, 2.5, 0.10, 10.0
DEFAULT_MAX_SECONDS = 15.0

CORRECTNESS, EFFICIENCY = "correctness", "efficiency"


def default_max_calls(expect: dict) -> int:
    """No tool: one model call. One expected tool: one tool round + the answer. Otherwise: 3."""
    tool = expect.get("tool")
    return 1 if tool == "none" else 2 if tool else 3


def check_turn(result, expect: dict, user_texts: list[str], seconds: float) -> list[tuple[str, str, str | None]]:
    """Returns [(check name, category, failure message or None)]."""
    out = []
    a = result.answer
    called = [c.name for c in result.tool_calls]
    C = CORRECTNESS

    tool = expect.get("tool")
    if tool == "none":
        out.append(("no tool call", C, f"called {called}" if called else None))
    elif tool:
        out.append((f"calls {tool}", C, None if tool in called else f"called {called or 'nothing'}"))
    if "args" in expect:
        out.append(("tool arguments", C, args_mismatch(result, expect["args"], tool if tool and tool != "none" else "rank_hubs")))
    if "ranking" in expect:
        want_null = expect["ranking"] is None or expect["ranking"] == "null"
        ok = (a.ranking is None) == want_null
        out.append((f"ranking {'null' if want_null else 'present'}", C, None if ok else f"ranking is {a.ranking and 'present'}"))
    if "confidence" in expect:
        out.append(("confidence", C, None if a.confidence in expect["confidence"] else f"got {a.confidence}"))
    for s in expect.get("mentions", []):
        out.append((f"mentions '{s}'", C, None if s.lower() in a.answer_markdown.lower() else "not in answer"))
    if expect.get("not_mentions"):
        text = f"{a.answer_markdown}\n{a.uncertainty_notes}"
        bad = mentions_forbidden(text, expect["not_mentions"])
        out.append(("forbidden phrases absent", C, f"answer contains {bad}" if bad else None))

    # Applied to every turn
    rank_args = expect.get("args") if tool == "rank_hubs" else None
    out.append(("ranking = engine", C, ranking_mismatch(result, rank_args)))
    bad = ungrounded_numbers(result, user_texts)
    out.append(("numbers grounded", C, f"not in tool results: {bad}" if bad else None))
    wrong = wrong_count_claims(result)
    out.append(("counts correct", C, f"wrong counts: {wrong}" if wrong else None))

    # Efficiency budget
    max_calls = expect.get("max_model_calls", default_max_calls(expect))
    out.append((f"model calls <= {max_calls}", EFFICIENCY,
                None if result.steps <= max_calls else f"{result.steps} model calls"))
    max_s = expect.get("max_seconds", DEFAULT_MAX_SECONDS)
    out.append((f"latency <= {max_s:g}s", EFFICIENCY, None if seconds <= max_s else f"{seconds:.1f}s"))
    return out


def cost_of(result) -> float:
    uncached = result.input_tokens - result.cache_read_tokens - result.cache_write_tokens
    return (uncached * PRICE_IN + result.cache_write_tokens * PRICE_CACHE_WRITE
            + result.cache_read_tokens * PRICE_CACHE_READ + result.output_tokens * PRICE_OUT) / 1e6


# ---------------------------------------------------------------- summary and comparison (pure)

def summarize(turns: list[dict]) -> dict:
    """Aggregate turn entries (possibly several repeats) into pass rates and totals."""
    by_check: dict[str, list[bool]] = defaultdict(list)
    cat_counts = {CORRECTNESS: [0, 0], EFFICIENCY: [0, 0]}
    secs, costs, calls = [], [], []
    for t in turns:
        if "error" in t:
            by_check[f"{t['case']}/turn{t['turn']}/agent error"].append(False)
            cat_counts[CORRECTNESS][1] += 1
            continue
        for name, (cat, err) in t["checks"].items():
            by_check[f"{t['case']}/turn{t['turn']}/{name}"].append(err is None)
            cat_counts[cat][0] += err is None
            cat_counts[cat][1] += 1
        secs.append(t["seconds"])
        costs.append(t["cost"])
        calls.append(t["steps"])
    pass_rates = {k: round(sum(v) / len(v), 3) for k, v in sorted(by_check.items())}
    q = statistics.quantiles(secs, n=20) if len(secs) >= 2 else secs * 19
    return {
        "correctness": {"passed": cat_counts[CORRECTNESS][0], "total": cat_counts[CORRECTNESS][1]},
        "efficiency": {"passed": cat_counts[EFFICIENCY][0], "total": cat_counts[EFFICIENCY][1]},
        "pass_rates": pass_rates,
        "latency_p50": round(statistics.median(secs), 1) if secs else None,
        "latency_p95": round(q[18], 1) if secs else None,
        "cost_total": round(sum(costs), 3),
        "cost_per_turn": round(statistics.mean(costs), 4) if costs else None,
        "model_calls_mean": round(statistics.mean(calls), 2) if calls else None,
    }


def compare(prev: dict, curr: dict) -> dict:
    """What changed between two summaries: checks that got worse or better, and the totals."""
    worse, better = [], []
    for k, rate in curr["pass_rates"].items():
        old = prev["pass_rates"].get(k)
        if old is None:
            continue
        if rate < old:
            worse.append((k, old, rate))
        elif rate > old:
            better.append((k, old, rate))
    delta = lambda key: (None if prev.get(key) is None or curr.get(key) is None
                         else round(curr[key] - prev[key], 4))
    return {"worse": worse, "better": better,
            "new_checks": sorted(set(curr["pass_rates"]) - set(prev["pass_rates"])),
            "cost_per_turn_change": delta("cost_per_turn"), "latency_p50_change": delta("latency_p50"),
            "model_calls_mean_change": delta("model_calls_mean")}


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=HERE.parent, timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# ---------------------------------------------------------------- run

def run_case(case: dict, repeat_no: int) -> list[dict]:
    history, user_texts, entries = [], [], []
    for i, turn in enumerate(case["turns"], 1):
        q = turn["question"]
        user_texts.append(q)
        entry = {"case": case["id"], "turn": i, "repeat": repeat_no, "question": q}
        t0 = time.perf_counter()
        try:
            result = ask(q, history)
        except AgentError as exc:
            print(f"  turn {i}: FAIL  agent error: {exc}")
            path = trajectory.save(q, case["id"], None, HERE / "trajectories", f"{case['id']}_turn{i}", error=exc)
            print(f"      trajectory of the failure: {path}")
            entries.append({**entry, "error": str(exc)})
            break
        secs = time.perf_counter() - t0
        checks = check_turn(result, turn.get("expect", {}), user_texts, secs)
        cost = cost_of(result)
        print(f"  turn {i}: \"{q}\"  ({secs:.1f}s, {result.steps} model calls, "
              f"{result.input_tokens}+{result.output_tokens} tokens, {result.cache_read_tokens} cached, ${cost:.3f})")
        for c in result.tool_calls:
            print(f"      tool @ model call {c.step}: {c.name}({json.dumps(c.input)})"
                  f"{'  [CACHED duplicate]' if c.cached else ''}{'  [ERROR]' if c.is_error else ''}")
        if not result.tool_calls:
            print("      tools: none")
        for name, cat, err in checks:
            tag = "PASS" if not err else ("FAIL" if cat == CORRECTNESS else "SLOW")
            print(f"    {tag}  {name}{'' if not err else '  -> ' + err}")
        trajectory.save(q, case["id"], result, HERE / "trajectories",
                        f"{case['id']}_turn{i}" + (f"_r{repeat_no}" if repeat_no > 1 else ""))
        entries.append({**entry, "seconds": round(secs, 1), "steps": result.steps, "cost": round(cost, 5),
                        "tokens": [result.input_tokens, result.output_tokens, result.cache_read_tokens],
                        "checks": {n: (cat, e) for n, cat, e in checks}, "answer": result.answer.model_dump(),
                        "tool_calls": [{"name": c.name, "input": c.input, "step": c.step, "cached": c.cached,
                                        "is_error": c.is_error} for c in result.tool_calls]})
        history += [{"role": "user", "content": q}, {"role": "assistant", "content": result.answer.model_dump_json()}]
    return entries


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--case", help="comma-separated case ids (default: all)")
    p.add_argument("--repeat", type=int, default=1, help="run each case N times and report pass rates")
    args = p.parse_args(argv)

    cases = yaml.safe_load((HERE / "cases.yaml").read_text(encoding="utf-8"))["cases"]
    if args.case:
        wanted = args.case.split(",")
        unknown = set(wanted) - {c["id"] for c in cases}
        if unknown:
            sys.exit(f"unknown case(s): {sorted(unknown)}")
        cases = [c for c in cases if c["id"] in wanted]
    service.load_results(restore=True)
    sys.stdout.reconfigure(encoding="utf-8")

    print(f"model: {settings.llm_model} (effort {settings.llm_effort}), repeat {args.repeat}\n")
    entries = []
    for r in range(1, args.repeat + 1):
        for case in cases:
            print(f"=== {case['id']}{f' (repeat {r})' if args.repeat > 1 else ''}: {' '.join(case['why'].split())}")
            entries += run_case(case, r)
            print()

    summary = summarize(entries)
    if args.repeat > 1:
        flaky = [(k, rate) for k, rate in summary["pass_rates"].items() if rate < 1]
        print("pass rate per check below 100% (across repeats):" + ("" if flaky else " none"))
        for k, rate in flaky:
            print(f"  {rate:>5.0%}  {k}")
        print()
    dupes = sum(1 for e in entries for c in e.get("tool_calls", []) if c["cached"])
    c, e = summary["correctness"], summary["efficiency"]
    print(f"CORRECTNESS: {c['passed']}/{c['total']} checks passed")
    print(f"EFFICIENCY:  {e['passed']}/{e['total']} within budget (model calls, latency); "
          f"{dupes} repeated tool calls; mean {summary['model_calls_mean']} model calls per turn")
    print(f"LATENCY:     p50 {summary['latency_p50']}s, p95 {summary['latency_p95']}s")
    print(f"COST:        ~${summary['cost_total']:.2f} total, ${summary['cost_per_turn']:.3f} per turn (list-price estimate)")

    run_info = {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": settings.llm_model,
                "effort": settings.llm_effort, "commit": _git_sha(), "repeat": args.repeat,
                "cases": [c["id"] for c in cases]}
    HISTORY.mkdir(exist_ok=True)
    previous = sorted(HISTORY.glob("*.json"))
    if previous:
        prev = json.loads(previous[-1].read_text(encoding="utf-8"))
        diff = compare(prev["summary"], summary)
        print(f"\nCOMPARED WITH {previous[-1].name} (commit {prev['run'].get('commit')}):")
        for k, old, new in diff["worse"]:
            print(f"  WORSE   {k}: {old:.0%} -> {new:.0%}")
        for k, old, new in diff["better"]:
            print(f"  BETTER  {k}: {old:.0%} -> {new:.0%}")
        if not diff["worse"] and not diff["better"]:
            print("  no check changed")
        print(f"  cost per turn {diff['cost_per_turn_change']:+}, latency p50 {diff['latency_p50_change']:+}s, "
              f"model calls {diff['model_calls_mean_change']:+}" if diff["cost_per_turn_change"] is not None else "")
        if diff["new_checks"]:
            print(f"  {len(diff['new_checks'])} new checks (not in the previous run)")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    record = {"run": run_info, "summary": summary, "turns": entries}
    (HISTORY / f"{stamp}_{run_info['commit']}.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    (HERE / "report.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    print(f"\nreport: {HERE / 'report.json'} · history: {HISTORY}")
    print(f"trajectories: {HERE / 'trajectories'} (view: uv run python -m app.agent.trajectory <file>)")
    sys.exit(1 if c["passed"] < c["total"] else 0)


if __name__ == "__main__":
    main()
