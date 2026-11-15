# Design decisions: API and agent layer

For each file in the API and agent layer: what it does, the decision it involved, and why it
was made that way. Decisions about data and scoring are in `docs/PLAN.md`, `docs/DATA_NOTES.md`
and the comments in `config/disruption.yaml`.

Files are listed in the order a request passes through them.

---

## 1. `app/agent/schemas.py`: the JSON formats between the LLM and our code

**What it does:** defines, as Pydantic classes, (a) the input of each tool (`RankHubsInput`) and (b) the format of the final answer (`AgentAnswer`: answer, ranking, assumptions, confidence, uncertainty notes, sources).

**Decision 1: write JSON schemas by hand, or generate them from Pydantic?**
Generate from Pydantic: **one definition** produces the schema Claude receives *and* validates what comes back. A hand-written schema and a separate validator inevitably drift apart, which would break "enforce a structured output format".

**Decision 2: optional fields with defaults, or every field required?**
Strict mode (which guarantees the model returns valid JSON) expects every field to be required. So every field is required, and fields that can be empty are nullable (`ranking: ... | None`). The upside: the model cannot silently skip `assumptions` or `confidence`, so uncertainty is reported **in every answer**, as the assignment asks.

**Decision 3: free text, or `Literal` values (`hazard`, `region`, `confidence`)?**
`Literal`. If the model sends `"volcano"` or `"midwest region"`, it is caught before reaching code. The list of valid values is part of the contract.

---

## 2. `app/agent/tools.py`: the tools the agent can call

**What it does:** `rank_hubs` calls the ranking engine and returns compact JSON: ranks, days per year, the main hazard, the top 2 drivers, caveats, and the method. The file also builds the tool definitions sent to Claude and validates arguments before running a tool.

**Decision 1: how much data to return to the model?**
- Everything (all hazards and drivers for 50 hubs): more tokens and cost, and the model gets lost in the detail.
- Only names and numbers: the model cannot explain *why*, and would start inventing reasons.
- Chosen: the main hazard, a short per-hazard split, and **the top 2 drivers**. Enough to explain "why Boston", while staying short.

**Decision 2: where should caveats live: in the prompt, or in the tool result?**
In the **result**. The coastal-wind caveat is attached only to the hubs it affects, and `method` explains the KPI and the periods. The knowledge lives in the data, not in a prompt that may not be read carefully, and the same tool serves both the agent and the evals.

**Decision 3: what happens when the model sends wrong arguments?**
Return an error to the model (`is_error`) with the details, rather than crashing. The model can then correct itself and try again.

**Decision 4: one general tool or several specific ones?**
For now there is one, because the slice is "most dangerous hubs". The `TOOLS` registry is built so `compare_hubs`, `explain_hub` and `weather_stat` can be added in later slices without changing the loop.

---

## 3. `app/agent/prompts.py`: the system prompt

**What it does:** sets the role and the working rules.

**Decision: how far to trust the model with the numbers?**
Not at all. The central rule is that **every number must come from a tool result**, and the ranking is copied exactly as the tool returned it, with no re-ranking. This is the assignment's "deterministic scoring, not LLM output" requirement turned into a rule the evals can check.
- Out-of-scope questions (other cities, forecasts) get an explicit answer with `confidence: low` instead of a guess.
- The prompt is short and principle-based rather than a long, over-specific list: current models follow clear principles better than long lists of examples.

---

## 4. `app/agent/loop.py`: the agent loop

**What it does:** sends the question to Claude, runs the tools it asks for, returns the results, and repeats until a final answer arrives. It then validates the answer and returns it with metadata (tools called, tokens, number of steps).

**Decision 1: a framework (LangChain/LangGraph) or a hand-written loop?**
A hand-written loop of about 100 lines. The flow is simple, so a framework would add abstraction, hidden prompts and dependency churn, all risky under a 24-hour deadline. Every line can be explained, and it is easy to debug.
The Anthropic SDK also has a "tool runner" that runs the loop for you. It was not used because explicit control over logging each tool call was wanted, and the evals need that.

**Decision 2: how to guarantee the answer format?**
Two layers: (a) structured output, where the API constrains the answer to the schema, and (b) Pydantic validation in code anyway. If it still fails, **one retry** with the error message, and after that a clear error rather than an infinite loop.
"Forced tool use" (forcing the model to call a "final answer" tool) was not used: Sonnet 5.5 rejects that option, and structured output is the cleaner route anyway.

**Decision 3: what if the model refuses?**
Server-side `fallbacks="default"` is enabled, so the API retries on a fallback model within the same call. If the whole chain refuses, the code returns a clear error and does not try to read an answer that does not exist. `max_tokens` (an answer cut off mid-way) gets the same treatment.

**Decision 4: protecting against infinite loops and cost.**
At most `MAX_STEPS = 6` model calls per question. The tokens used are counted and returned, so the cost per question is visible.

**Decision 5: where the API key comes from.**
The settings library reads `.env` but **does not** pass its values to the Anthropic SDK, so the key is passed explicitly. There is also a second name, `AGENT_ANTHROPIC_API_KEY`, so a cloud environment can set a key for the agent without interfering with a tool that reads plain `ANTHROPIC_API_KEY`.

**Model choice:** `claude-sonnet-5-5` at `low` effort (configurable with `LLM_MODEL`). The tools do the computing; the model only chooses tools and explains, so a smaller model at low effort is enough and keeps chat latency and cost down.

---

## 5. `app/scoring/service.py`: the current scores, kept in memory

**What it does:** computes the scores for all 50 hubs once (at startup) and keeps them in memory.

**Decision: compute on every request, store in a table, or keep in memory?**
- Computing on every request repeats the same work: about a second of pandas processing per request.
- Storing in a table adds a layer that has to be kept in sync.
- Chosen: **compute once at startup, keep in memory**. The data only changes when a new download happens, and recomputing takes a second. It is the simplest solution that is also fast.

---

## 6. `app/main.py`: the FastAPI service

**What it does:**
- **startup:** snapshot → SQLite → scores
- **`/health`**, **`/rankings`**, **`/hubs/{id}`:** deterministic, no LLM
- **`/chat`:** the agent, with conversation history

**Decision 1: why expose deterministic endpoints too, and not just chat?**
They show the scoring stands on its own without the LLM, which is an explicit assignment requirement. They are also a reference point: the eval compares the agent's ranking with `/rankings`. And they are the fastest way to show something live if the LLM is slow.

**Decision 2: how to store the conversation for follow-up questions?**
- Option A: store everything, including tool calls and the model's thinking blocks. That gives the most context, but those blocks are tied to the specific model and conversation, and it bloats every request.
- Option B: store **only the question and the final answer** as plain text.
- Chosen: B. It is simple, robust, and works even if the model is switched. For a follow-up like "and what about Chicago?", the model simply calls the tool again, so the numbers stay fresh and correct.
- History is capped at the last 6 turns, to limit cost.

**Decision 3: errors.**
An agent failure returns HTTP 502 with a clear message, rather than a 500 crash, so the UI can show something sensible.

---

## 7. Small changes to existing files

- **`app/db.py`:** a new `conversations` table. Conversation memory lives in the same SQLite file; there was no reason to add another service.
- **`app/settings.py`:** the two API key fields (see section 4).
- **`app/scoring/report.py`:** uses `service` instead of its own copy of the loading logic. That removes the duplication, so the report and the API compute exactly the same thing.

---

## 8. `tests/test_api_agent.py`: tests for the API and agent

**Decision: how do you test an LLM agent without calling the LLM?**
A **scripted fake Claude client** returns preset responses (a tool call, then an answer). This tests **our code**, the loop, the validation, the retry, refusal handling and history, quickly, for free, and with the same result every time.
Whether *the model itself* chooses well is a different question, and that is what the **evals** are for, using real API calls. The separation is deliberate: tests check the code, evals check the model's behaviour.
The tests also confirm the schemas are fully strict (every field required, no extra fields), so a future change cannot quietly break the format contract.

---

## 9. Changes after eval run 1 (see `docs/EVALS.md`)

**Decision: should the model write the ranking at all?**
Run 1 showed the model re-typing a 50-hub ranking as JSON (~1,850 output tokens) plus a table, taking 22 seconds. The model was copying data our code already had, which is slow, costs money, and can introduce copy errors (that is why the `ranking = engine` check existed).
Chosen: split the schema. `ModelAnswer` (what the LLM must produce) holds only the explanation, assumptions, confidence, uncertainty and sources. `AgentAnswer` (what the API returns) adds `ranking`, attached by the server from the last `rank_hubs` result. The JSON-schema contract between LLM and code still holds; the data path no longer passes through the LLM at all.

**Decision: how to stop duplicate tool calls?**
A prompt rule alone depends on the model obeying it. Chosen: both a prompt rule and a per-question cache in the loop. Tools are deterministic, so a repeated call gets a short "already answered" result instead of the full JSON again.

**Decision: fix the "5 years" grounding failure in the model or in the data?**
Loosening the check would hide real problems. Chosen: put the fact in the tool's `method` (`weather_years: 5`), so the number has a source.

---

## 10. Chat UI: `app/static/index.html` (served by `GET /` in `app/main.py`)

**Decision 1: which technology?**
- Streamlit: quick in Python, but a second process and server, its own look and behaviour, and two services to deploy.
- React / Vue: flexible, but Node, a build step and many dependencies for one page.
- Chosen: **one static HTML page served by FastAPI**. One process and one URL (simple deployment); the page is a plain client of `POST /chat` with no internal shortcut, which honestly meets "a chat interface that uses the API"; no build step; voice can later use the browser's Web Speech API with no server changes. Same origin as the API, so no CORS configuration.

**Decision 2: a Markdown library, or none?**
The model's text is **untrusted input**. Rendering it directly could allow injected `<script>` (XSS). Chosen: a ~30-line renderer that **escapes everything first**, then formats `**bold**`, bullet lists and pipe tables. No CDN dependency (works on a blocked or slow network). Trade-off: only that Markdown subset; the prompt asks for exactly that format.

**Decision 3: one view or two?**
- The chat bubble shows what the **model said** (explanation + a table of at most 10 rows).
- The details panel shows what the **engine computed** and how: the full ranking from `answer.ranking` (attached by the server) with bars, confidence badge, assumptions, uncertainty, sources, which tools ran with which arguments (and cache hits), raw tool results (collapsed), token usage.
Why: the analyst can tell the model's wording from the engine's data, and assumptions and uncertainty are always visible next to every answer instead of buried in prose. The ranking table is built from structured data, never by parsing the model's Markdown.

**Decision 4: where to keep the conversation?**
The `session_id` lives in page memory; the history lives on the server (SQLite). "New conversation" resets it. Trade-off: reloading the page starts a new conversation; accepted for simplicity.

**Decision 5: streaming?**
The answer is structured JSON that only means something when complete; streaming it would add partial-JSON handling on both sides. Chosen: no streaming; a "Thinking..." indicator and a disabled Send button (no double submits) for the 3-10 s wait. Server errors are shown with the server's own message (e.g. "Anthropic API rate limit (429)..."), and an unreachable server separately.

**Decision 6: guiding the user.**
Three clickable example questions show what the agent can do in this slice (only `rank_hubs` exists), so scope is communicated up front.

**Decision 7: phones, dark mode, accessibility.**
Two columns on desktop, stacked under 860 px; ranking bars hidden on phones. A bug found in testing: the page was 492 px wide on a 390 px phone because a grid column defined as `1fr` grows to its widest content; fixed with `minmax(0, 1fr)`, and wide chat tables scroll inside their bubble. Dark mode follows the OS setting via CSS variables. Labels on inputs, `aria-live` on messages, Enter to send, Shift+Enter for a new line.

**Decision 8: testing without an API key.**
Headless Chromium with `/chat` intercepted: a canned answer built from a real answer, then an injected 502/429 error. Checked at desktop and iPhone 13 sizes, including page width against screen width (which found the bug in decision 7). An API test checks that `/` serves the page and that it calls `/chat`.

**Known limitations:** no streaming; a conversation does not survive a reload; a Markdown subset only; voice not implemented yet.
