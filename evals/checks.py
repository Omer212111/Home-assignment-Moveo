"""Eval checks: pure functions over the agent's result (no API calls), so they are unit-tested."""
import json
import re

from app.agent.loop import AgentResult
from app.agent.schemas import RankHubsInput
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import TOOLS, rank_hubs

NUMBER = re.compile(r"\d+(?:\.\d+)?")


def numbers_in_text(text: str) -> list[str]:
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)  # 1,826 -> 1826
    return NUMBER.findall(text)


def _all_numbers(obj) -> list[float]:
    """Every number in a tool result: numeric values and numbers inside strings (rule labels)."""
    out = []
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, str):
        out += [float(n) for n in numbers_in_text(obj)]
    elif isinstance(obj, dict):
        for v in obj.values():
            out += _all_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _all_numbers(v)
    return out


def _matches(token: str, sources: list[float]) -> bool:
    """`token` is grounded if some source number rounds to it (13.07 matches 13.071)."""
    value = float(token)
    decimals = len(token.split(".")[1]) if "." in token else 0
    tol = 0.5 * 10 ** -decimals + 1e-9
    return any(abs(value - s) <= tol for s in sources)


def ungrounded_numbers(result: AgentResult, user_texts: list[str]) -> list[str]:
    """Numbers in the answer text that appear neither in a tool result nor in the user's questions.

    Allowed sources: tool results, the user's questions, and the facts we give the model
    ourselves (system prompt and tool descriptions, e.g. "50 hubs").

    Limitations: small integers (1-50) appear in every ranking as ranks, so a wrong count like
    "8 of the 10" passes this check (wrong_count_claims covers that); and a low-precision number
    such as "3.5" can match an unrelated tool value by coincidence.
    """
    sources = [n for c in result.tool_calls for n in _all_numbers(c.result)]
    sources += _all_numbers(SYSTEM_PROMPT) + _all_numbers([desc for _, _, desc in TOOLS.values()])
    sources += [float(n) for t in user_texts for n in numbers_in_text(t)]
    text = f"{result.answer.answer_markdown}\n{result.answer.uncertainty_notes}"
    return [t for t in numbers_in_text(text) if not _matches(t, sources)]


COUNT_CLAIM = re.compile(r"\b(\d+)\s+(?:out\s+)?of\s+(?:the\s+)?(\d+)\b", re.IGNORECASE)


def wrong_count_claims(result: AgentResult) -> list[str]:
    """'X of the Y' phrases where X is not one of the tool's pre-computed counts."""
    rank_calls = [c for c in result.tool_calls if c.name == "rank_hubs" and not c.is_error]
    if not rank_calls:
        return []
    summary = rank_calls[-1].result.get("summary", {})
    counts = set(summary.get("hubs_by_main_hazard", {}).values())
    returned = summary.get("hubs_returned")
    bad = []
    for x, y in COUNT_CLAIM.findall(result.answer.answer_markdown):
        if int(y) == returned and int(x) not in counts | {returned}:
            bad.append(f"{x} of {y}")
    return bad


def ranking_mismatch(result: AgentResult, expected_args: dict | None = None) -> str | None:
    """The ranking shown to the user must equal the engine's ranking.

    With the case's expected rank_hubs arguments: compared with the engine's ranking for the call
    that used those arguments, i.e. what the user should see. This does not depend on the server's
    own selection rule (eval run 8: the model called top_n=50 then 10; the server correctly showed
    50, and the old check, which looked only at the last call, failed it).
    Without expected arguments: a wiring check against the call the server attaches."""
    rank_calls = [c for c in result.tool_calls if c.name == "rank_hubs" and not c.is_error]
    if result.answer.ranking is None:
        return None if not rank_calls else "tool was called but answer.ranking is null"
    if not rank_calls:
        return "answer has a ranking but rank_hubs was never called"
    matching = [c for c in rank_calls
                if expected_args and all(_same(c.input.get(k), v) for k, v in expected_args.items())]
    if matching:
        source = max(matching, key=lambda c: c.input.get("top_n", 0))
    else:  # same rule as the server: the largest list with the filters of the last call
        filters = lambda c: {k: v for k, v in c.input.items() if k != "top_n"}
        source = max((c for c in rank_calls if filters(c) == filters(rank_calls[-1])),
                     key=lambda c: c.input.get("top_n", 0))
    expected = rank_hubs(RankHubsInput.model_validate(source.input))["ranking"]
    got = [(r.hub_id, round(r.days_per_year, 2)) for r in result.answer.ranking]
    want = [(r["hub_id"], round(r["days_per_year"], 2)) for r in expected]
    if got != want:
        return f"answer ranking ({len(got)} rows) {got[:3]}... != engine ranking ({len(want)} rows) {want[:3]}..."
    return None


def _same(actual, expected) -> bool:
    # Lists of hubs or hazards are sets: ["houston", "miami"] equals ["miami", "houston"].
    if isinstance(expected, list) and isinstance(actual, list):
        return sorted(actual) == sorted(expected)
    return actual == expected


def args_mismatch(result: AgentResult, expected_args: dict, tool: str = "rank_hubs") -> str | None:
    """Passes if ANY call of the tool used the expected arguments (extra calls are an efficiency
    matter, reported separately). On failure, shows the closest call's differences."""
    calls = [c for c in result.tool_calls if c.name == tool]
    if not calls:
        return f"{tool} was not called"
    diffs = [{k: (c.input.get(k), v) for k, v in expected_args.items() if not _same(c.input.get(k), v)}
             for c in calls]
    if any(not d for d in diffs):
        return None
    diff = min(diffs, key=len)
    return f"args (got, expected): {json.dumps(diff)}"


def mentions_forbidden(text: str, phrases: list[str]) -> list[str]:
    """Phrases that must NOT appear (prompt injection, system-prompt leaks, invented money)."""
    low = text.lower()
    return [p for p in phrases if p.lower() in low]
