"""The agent loop: Claude chooses tools, our code runs them, Claude writes a structured answer.

A hand-written loop (no framework): call the model -> run the tools it asked for ->
send results back -> repeat until it returns the final AgentAnswer JSON.
"""
import json
import time

import anthropic
from pydantic import BaseModel, ValidationError

from app.agent.prompts import SYSTEM_PROMPT
from app.agent.schemas import AgentAnswer
from app.agent.tools import run_tool, tool_definitions
from app.settings import settings

MAX_STEPS = 6          # model calls per question (tool rounds + final answer)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ToolCall(BaseModel):
    name: str
    input: dict
    is_error: bool
    duration_ms: int


class AgentResult(BaseModel):
    answer: AgentAnswer
    tool_calls: list[ToolCall]
    model: str
    steps: int
    input_tokens: int
    output_tokens: int


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
            output_format=AgentAnswer,
            output_config={"effort": settings.llm_effort},
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


def ask(question: str, history: list[dict] | None = None) -> AgentResult:
    """Answer one question. `history` is earlier turns as plain {role, content: str} messages."""
    messages = [*(history or []), {"role": "user", "content": question}]
    calls: list[ToolCall] = []
    tokens_in = tokens_out = 0
    retried_schema = False

    for step in range(1, MAX_STEPS + 1):
        response = _call_model(messages)
        tokens_in += response.usage.input_tokens
        tokens_out += response.usage.output_tokens

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
                t0 = time.perf_counter()
                result, is_error = run_tool(block.name, block.input)
                calls.append(ToolCall(name=block.name, input=block.input, is_error=is_error,
                                      duration_ms=int((time.perf_counter() - t0) * 1000)))
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(result), "is_error": is_error})
            messages.append({"role": "user", "content": results})  # all results in one message
            continue

        # Final answer: structured output should guarantee valid JSON; validate anyway.
        text = _final_text(response.content)
        try:
            answer = AgentAnswer.model_validate_json(text or "")
        except ValidationError as exc:
            if retried_schema:
                raise AgentError(f"Answer did not match the schema after a retry: {exc}") from exc
            retried_schema = True
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content":
                             f"Your answer did not match the required JSON schema: {exc}. Answer again."})
            continue
        return AgentResult(answer=answer, tool_calls=calls, model=response.model, steps=step,
                           input_tokens=tokens_in, output_tokens=tokens_out)

    raise AgentError(f"No final answer after {MAX_STEPS} model calls.")
