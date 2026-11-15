"""The agent loop: Claude chooses tools, our code runs them, Claude writes a structured answer.

A hand-written loop (no framework): call the model -> run the tools it asked for ->
send results back -> repeat until it returns the final AgentAnswer JSON.
"""
import json
import time

import anthropic
from pydantic import BaseModel, ValidationError

from app.agent.prompts import SYSTEM_PROMPT
from app.agent.schemas import AgentAnswer, ModelAnswer, RankedHub
from app.agent import trajectory as traj
from app.agent.tools import run_tool, tool_definitions
from app.settings import settings

MAX_STEPS = 6          # model calls per question (tool rounds + final answer)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ToolCall(BaseModel):
    name: str
    input: dict
    is_error: bool
    duration_ms: int
    cached: bool = False  # True when an identical earlier call in this question was reused
    step: int = 0         # model call that requested it (same step = same response)
    result: dict  # what the model saw (used by the evals' grounding check and the UI)


class AgentResult(BaseModel):
    answer: AgentAnswer
    tool_calls: list[ToolCall]
    model: str
    steps: int
    input_tokens: int
    output_tokens: int
    trajectory: list[dict] = []  # step-by-step events, see app/agent/trajectory.py


class AgentError(RuntimeError):
    pass


_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        key = settings.agent_anthropic_api_key or settings.anthropic_api_key
        if not key:
            raise AgentError("No API key: set ANTHROPIC_API_KEY (or AGENT_ANTHROPIC_API_KEY) in .env")
        _client = anthropic.Anthropic(api_key=key)
    return _client


def _final_text(content) -> str | None:
    texts = [b.text for b in content if getattr(b, "type", None) == "text"]
    return texts[-1] if texts else None


def _call_model(messages: list) -> object:
    """One Messages API call. API failures become AgentError with a readable message."""
    try:
        return client().beta.messages.parse(
            model=settings.llm_model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=tool_definitions(),
            output_format=ModelAnswer,
            output_config={"effort": settings.llm_effort},
            thinking={"type": "adaptive", "display": settings.llm_thinking_display},
            betas=[FALLBACK_BETA],
            fallbacks="default",  # if the model declines, the API retries on a fallback model
        )
    except anthropic.AuthenticationError as exc:
        raise AgentError("Anthropic API rejected the key (401): check ANTHROPIC_API_KEY in .env") from exc
    except anthropic.RateLimitError as exc:
        raise AgentError("Anthropic API rate limit (429): wait a moment and retry") from exc
    except anthropic.APIStatusError as exc:
        raise AgentError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AgentError(f"Cannot reach the Anthropic API: {exc}") from exc


def _ranking_from(calls: list[ToolCall]) -> list[RankedHub] | None:
    """The ranking from the last successful rank_hubs call, taken from the engine's result."""
    for call in reversed(calls):
        if call.name == "rank_hubs" and not call.is_error:
            return [RankedHub(**{k: row[k] for k in RankedHub.model_fields}) for row in call.result["ranking"]]
    return None


def ask(question: str, history: list[dict] | None = None) -> AgentResult:
    """Answer one question. `history` is earlier turns as plain {role, content: str} messages."""
    messages = [*(history or []), {"role": "user", "content": question}]
    calls: list[ToolCall] = []
    tokens_in = tokens_out = 0
    retried_schema = False
    cache: dict[tuple[str, str], tuple[dict, bool]] = {}
    events: list[dict] = []

    for step in range(1, MAX_STEPS + 1):
        t_call = time.perf_counter()
        response = _call_model(messages)
        tokens_in += response.usage.input_tokens
        tokens_out += response.usage.output_tokens
        events.append(traj.model_call_event(step, response, int((time.perf_counter() - t_call) * 1000)))

        if response.stop_reason == "refusal":
            raise AgentError("The model declined to answer this request.")
        if response.stop_reason == "max_tokens":
            raise AgentError("The answer was cut off (max_tokens).")

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                # Identical calls within one question are served from a cache: tools are
                # deterministic, so a repeat adds tokens but no information (seen in eval run 1).
                key = (block.name, json.dumps(block.input, sort_keys=True))
                t0 = time.perf_counter()
                cached = key in cache
                if not cached:
                    cache[key] = run_tool(block.name, block.input)
                result, is_error = cache[key]
                calls.append(ToolCall(name=block.name, input=block.input, is_error=is_error, cached=cached, step=step,
                                      duration_ms=int((time.perf_counter() - t0) * 1000), result=result))
                events.append({"type": "tool_result", "step": step, "name": block.name, "input": block.input,
                               "cached": cached, "is_error": is_error, "duration_ms": calls[-1].duration_ms,
                               "summary": traj.result_summary(block.name, result, is_error)})
                content = (json.dumps({"note": "identical call already answered above; same result"})
                           if cached else json.dumps(result))
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})  # all results in one message
            continue

        # Final answer: structured output should guarantee valid JSON; validate anyway.
        text = _final_text(response.content)
        try:
            model_answer = ModelAnswer.model_validate_json(text or "")
            answer = AgentAnswer(**model_answer.model_dump(), ranking=_ranking_from(calls))
        except ValidationError as exc:
            if retried_schema:
                raise AgentError(f"Answer did not match the schema after a retry: {exc}") from exc
            retried_schema = True
            events.append({"type": "schema_retry", "step": step, "error": str(exc)})
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content":
                             f"Your answer did not match the required JSON schema: {exc}. Answer again."})
            continue
        events.append({"type": "final_answer", "step": step, "confidence": answer.confidence})
        return AgentResult(answer=answer, tool_calls=calls, model=response.model, steps=step,
                           input_tokens=tokens_in, output_tokens=tokens_out, trajectory=events)

    raise AgentError(f"No final answer after {MAX_STEPS} model calls.")
