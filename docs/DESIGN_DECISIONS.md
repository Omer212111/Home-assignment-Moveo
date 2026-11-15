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
- *Superseded by section 17:* the scores now live in database tables, versioned by scoring run; memory is only a cache of the active run.

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
Clickable example questions show what the agent can do (ranking, comparison, explanation), so scope is communicated up front.

**Decision 7: phones, dark mode, accessibility.**
Two columns on desktop, stacked under 860 px; ranking bars hidden on phones. A bug found in testing: the page was 492 px wide on a 390 px phone because a grid column defined as `1fr` grows to its widest content; fixed with `minmax(0, 1fr)`, and wide chat tables scroll inside their bubble. Dark mode follows the OS setting via CSS variables. Labels on inputs, `aria-live` on messages, Enter to send, Shift+Enter for a new line.

**Decision 8: testing without an API key.**
Headless Chromium with `/chat` intercepted: a canned answer built from a real answer, then an injected 502/429 error. Checked at desktop and iPhone 13 sizes, including page width against screen width (which found the bug in decision 7). An API test checks that `/` serves the page and that it calls `/chat`.

**Known limitations:** no streaming; a conversation does not survive a reload; a Markdown subset only; voice not implemented yet.

---

## 11. Slice 3: `explain_hub` and `compare_hubs` (`app/agent/tools.py`)

For the example questions "Compare Miami and Houston in terms of hurricane and flood exposure" and
"Why is the Dallas hub's weather disruption risk high?".

**Decision 1: two tools, or one general "hub details" tool?**
Two tools with clear names that match the two intents. The model picks a tool by its name and
description, so "compare" -> `compare_hubs` and "why" -> `explain_hub` leave little room for
error. Both share one builder (`_hazard_detail`), so the numbers cannot differ between them.

**Decision 2: how does the model name a hub?**
`hub_id` is an enum of the 50 ids, generated from `config/hubs.yaml`. A non-hub ("paris") is
rejected by the schema, and the model must resolve "Arlington" to `arlington_tx` itself.

**Decision 3: the "why is Dallas high" premise.**
Dallas is #17 of 50 (6.08 days/yr vs a median of 4.85): above the median, not near the top.
Answering "why it is high" without saying so would be misleading for an investment decision.
`explain_hub` therefore returns pre-computed context (overall rank, position, median, difference
from the median), and the prompt asks the model to correct a premise the data does not support,
then explain the drivers. The eval checks that the answer mentions the rank (17).

**Decision 4: no arithmetic in the model (same principle as `rank_hubs`).**
`compare_hubs` returns, per hazard, the highest and lowest hub and the difference, plus the
combined total of the selected hazards. `explain_hub` returns each hazard's share of the total.

**Decision 5: say what each number is built from.**
Hurricane days come from 20 years of FEMA declarations; flood days come from 5 years of rainfall;
flooding during a declared hurricane counts under hurricanes. Each hazard carries a `built_from`
note, so a comparison of "hurricane and flood" states that the two use different data.
Example: Miami has more hurricane days (1.40 vs 1.05) and flood days (2.60 vs 2.50), yet
Houston's total is higher (4.67 vs 4.20) because of winter and severe-storm days.

**Decision 6: the UI.**
The details panel builds a hubs x hazards table (days and rank) from the tool result, like the
ranking table, never by parsing the model's text. `AgentAnswer` is unchanged: the comparison is
in `tool_calls`, which the API already returns.

---

## 12. Agent trajectories: how the agent thought and acted (`app/agent/trajectory.py`)

**What it does:** the loop records every step of a question as events: each model call (a
summary of the model's thinking, the tools it asked for, time, tokens, stop reason), each tool run
(arguments, a one-line summary of the result, cache hit, error), schema retries and the accepted
answer. The events are returned by `/chat` (`result.trajectory`), saved as one JSON file per
question in `data/trajectories/` (git-ignored), saved per eval turn in `evals/trajectories/`, and
printed as a readable timeline with `uv run python -m app.agent.trajectory [file]`.

**Decision 1: where does "how the agent thinks" come from?**
Sonnet 5.5 always uses adaptive thinking, but the API returns the thinking text empty by default
(`display: "omitted"`). We request `display: "summarized"`: a readable summary of the reasoning.
Billing is the same either way; it is configurable (`LLM_THINKING_DISPLAY`). Limits stated
honestly: it is a *summary*, not the raw chain of thought, and at effort `low` the model may think
very little on simple steps, so some steps show no thinking.

**Decision 2: one log format for chat, evals and the UI.**
The same event list feeds the API, the files, the CLI viewer and the evals, so a failing eval case
can be read step by step exactly as a user would see it in the chat. This is how eval run 3's
duplicate calls would have been diagnosed directly ("model call 2 calls rank_hubs again").

**Decision 3: JSON files, not a database table.**
Trajectories are a debugging and review aid, not product data: files are easy to open, diff,
attach to a bug report and commit for the eval runs. Saving can never fail an answer (errors are
logged). Trade-off: no querying across questions; a table (or OpenTelemetry tracing) would be the
production step.

**Decision 4: what is left out.**
Full tool results are not repeated in the trajectory (they are in `tool_calls` and can be large);
the trajectory keeps a one-line summary per tool run. Conversation history across turns still
stores only question and answer text (section 6), so thinking blocks never leak into later turns.

**Decision 5: the UI.**
An "Agent steps" switch in the header (remembered in the browser) shows a collapsible timeline
under each answer: thinking summary, tool calls, results, retries. Off by default, so analysts see
a clean chat and reviewers can turn the reasoning on. Found in browser testing: on a phone the
example buttons left only 182 px for the conversation; they are now hidden on phones once the
conversation has started.

---

## 13. Guardrail against repeated tool calls (after eval run 4)

**Decision: rely on the prompt, or enforce in code?** Two prompt changes (runs 2 and 3) did not
stop the model from repeating identical calls, and in run 4 it looped until `MAX_STEPS`. Enforced
in code: after a round of only repeated calls, and on the last allowed call, the request sets
`tool_choice: {"type": "none"}`, so the model can only answer. (Forcing a specific tool is
rejected by Sonnet 5.5; `none` is supported.)
**Why not disable tools after the first round?** That would block questions needing two different
tools in sequence (rank, then explain the top hub). Only *repeats* trigger the guardrail.
**Why keep the tools in the request?** Removing them would change the request prefix and defeat
prompt caching; `tool_choice` leaves the prefix unchanged.

---

## 14. The time dimension: months, seasons, years, raw weather (`app/scoring/periods.py`)

Driven by `docs/QUESTION_CATALOG.md`: analysts ask *when* (which month, which season, which year),
not only "how many days per year".

**Decision 1: compute breakdowns from the same records as the annual total.**
The engine already built a per-day disruption list and then threw it away. It now keeps it
(`HubDisruption.records`, excluded from the API response): weather days with their severity, and
each counted FEMA disaster placed on its incident start date. Every breakdown sums these records,
so the 12 months and the 4 seasons add up to the annual total, for every hub and hazard (a test
checks all 50 x 7). A separate monthly model would have drifted from the annual number.

**Decision 2: two kinds of time numbers, labelled differently.**
- By month / season: *expected days per year in that period* (weather averaged over 5 years,
  disasters over 20), so they add up to the KPI.
- By year: *actual days in that calendar year* (disasters at their full days). This answers "was
  2024 worse than 2021?"; every result carries a small-sample warning, because 5 years show
  variability, not a trend (Denver ranges from 5.8 to 14.5 days).

**Decision 3: one `period` contract for every tool** (`season` or `months`, never both; validated).
Meteorological seasons (winter = Dec-Feb), stated in every result that uses them. Hurricane
season is expressed as months 6-11 rather than a fifth "season", so seasons stay a partition of
the year.

**Decision 4: "winter" the hazard vs "winter" the season.** The schema descriptions and the prompt
spell out the difference ("winter storms" = hazard; "in winter" = period), and two eval cases
test exactly that pair.

**Decision 5: `weather_stat` reports raw weather, not disruption.** "What percentage of days had
snowfall" is a weather fact, so it reads the daily data directly and returns the exact condition,
date range and number of days used. For "days with snowfall" it returns both > 0 cm (43 days,
11.8% in Denver 2025) and >= 1 cm (17 days, 4.7%), because the definition changes the answer by a
factor of 2.5 and trace amounts are reanalysis noise. "Last year" = `latest_full_year` (2025).

**Decision 6: tool count.** Five tools, each named after a question type (rank, explain, compare,
breakdown, weather). Adding `period` to the existing tools instead of new "seasonal_rank" /
"seasonal_compare" tools keeps the choice simple for the model.

Also found while testing: an invalid-argument error from a model validator contained a Python
exception object, which would have crashed the loop when sent back to the model; errors are now
returned without that context.

---

## 15. Tool rounds, prompt caching and ranking attach (after eval run 6)

**The evidence.** Run 6 repeated an identical call in 14 of 17 turns. The trajectory of
`safest_hubs` shows model call 2 with **no thinking and no text**: the same call re-sent in 1.8 s.
Not a reasoned choice, so prompt wording (tried in runs 2 and 3) cannot be relied on.

**Options considered** (with the user):
| Option | Stops repeats | Stops wandering | Chains (B needs A's result) | Prompt cache |
|---|---|---|---|---|
| ReAct, one tool per step | no (it is today's loop; the model decides when to stop) | no | yes | intact |
| Remove the just-used tool from the next request | yes | no | yes | broken (tool list changes) |
| One parallel round, then answer | yes | yes | no | intact |
| **At most two rounds + retry after an error** (chosen) | repeats cost at most one round | bounded | yes | intact |

**Chosen:** at most `MAX_TOOL_ROUNDS = 2` successful tool rounds; then `tool_choice: none` forces
the answer. A round in which a tool returned an error does not count, so the model can correct
invalid arguments (bounded by `MAX_STEPS = 6`). The repeated-only guardrail (section 13) stays.
The tool list never changes, so the cached prefix stays valid. The prompt asks for all
independent tools in the first response.

**Prompt caching:** top-level `cache_control: {type: "ephemeral"}` (automatic caching). The tools
+ system prompt (~6k tokens) are identical for every call and every question, and within one
question each call re-sends the previous messages; cached reads cost ~5% of the input price.
Token counts now include cache reads and writes, and the eval cost estimate prices them separately.

**Ranking attach (fix A):** among `rank_hubs` calls with the same filters as the last one, the
server attaches the one with the largest `top_n` (run 3: 50 then 10 showed 10 hubs).

**"Last year":** the `year` field's description now names the year (2025), so the model does not
have to look up `latest_full_year` first (run 6: it first called with no year).

---

## 16. Raw API cache in Redis (`app/sources/http_cache.py`)

**Decision (user's choice): Redis as the raw HTTP cache backend, files kept as the fallback.**
When `REDIS_URL` is set, every public-API response is stored in Redis under
`httpcache:<source>:<hash>` (the same hash as the file name, API key excluded), as a JSON envelope
`{url, params, fetched_at, data}`. When it is not set, the cache uses `data/raw/` files as before.

Why this shape:
- **Same keys in both backends**, so `uv run python -m app.sources.http_cache import-files` moves the
  97 committed responses into Redis and ingestion makes no API calls (verified against a real Redis:
  97 imported, full ingest with the HTTP client disabled, identical snapshot tables).
- **No expiry** (`TTL -1`): historical weather and declarations do not change. Redis must run with
  persistence (`--appendonly yes`), otherwise a restart empties the cache and the next ingest
  spends the Open-Meteo quota again.
- **The envelope adds what files did not have**: the request URL, parameters and fetch time, so the
  audit question "where did this number come from?" is answered from Redis itself.
- **A configured but unreachable Redis is an error**, not a silent fall-back to files: a fall-back
  would hide the misconfiguration and quietly call the APIs.
- **Files stay the default** so the repository works on a fresh clone and in tests with no server
  (tests use `fakeredis`).

What Redis adds over files: one cache shared by every machine or ingestion worker pointing at it,
and a natural home for the planned live data (NWS alerts with a short TTL). What it costs: a server
to run (Docker locally; a managed Redis when deployed) and the cache is no longer visible in git.
The running API does not use this cache at all; it starts from the parquet snapshot.

---

## 17. Scores in database tables, versioned by scoring run (`app/scoring/store.py`)

**Decision (user's choice): the scores move from memory into SQLite tables**: `rule_sets`,
`scoring_runs`, `hub_results`, `hazard_scores`, `disruption_days` (`docs/DATA_MODEL.md`).
The unused early `hub_scores` table is dropped.

Measured before deciding the details: full scoring 1.1 s, writing a run 0.02 s, loading a run
0.05 s; a run is 50 + 300 + 2,712 rows.

How it works:
- **A scoring run records what produced it**: the hash and full text of `config/disruption.yaml`
  (`rule_sets`) and a fingerprint of the snapshot files. At startup, if the active run matches
  the current rules and data it is loaded; otherwise the engine scores and stores a new run. The
  second start of an unchanged repository therefore does no scoring.
- **Activation is atomic**: the inserts and the switch of `active` happen in one transaction.
- **Memory is a cache of the active run, not the source of truth.** Tools still read in-memory
  objects (0.6 ms per ranking), but every 5 s the service checks for a newer active run and
  reloads it. This removes the old limitation that new scores needed a restart:
  `uv run python -m app.scoring.store --new` after editing the rules reaches a running server.
- **Traceability**: every tool result carries `scoring_run`; `/health` shows it with the data date.
- **Comparison**: `--diff OLD NEW` lists hubs and hazards whose days per year changed, which is
  the core of the risk-change alert bonus.

Why not query the tables on every tool call: rankings, ranks and breakdowns need all hubs; reading
3,000 rows and rebuilding objects per call (~50 ms) buys nothing over a cache that is invalidated
by run id. With Postgres and several app replicas (`docs/DATA_MODEL_SCALABLE.md`), each replica
caches the same active run, so all replicas answer from the same scores.

Why the scoring tables are not in the parquet snapshot: they are derived from the snapshot and the
rules, so committing them would create a second source of truth; a fresh clone scores once on its
first start (~1 s).

---

## 18. Standardised index and risk tiers (`config/disruption.yaml` -> `index`)

**Decision: keep days per year as the KPI and add a standardised index and a tier next to it**
(a presentation layer, not a replacement: days are a business unit, add up across hazards and can
later be priced).

- **Index = 100 x days / reference_days**, with **one reference for the total and for every hazard**:
  4.85 days/yr, the median hub's total (2021-2025, calibration v2). Dallas 6.08 days -> 125 ("25%
  above a typical hub"); Phoenix heat 4.5 days -> 93 ("heat alone costs 93% of a typical hub's year").
  Per-hazard references were rejected: the median hub has 0 heat and 0 tropical days (division by
  zero), and a mean reference makes rare hazards explode (Phoenix heat would be ~1,300).
  With one reference, hazard indices add up to the total index, exactly like the days.
- **Fixed reference, not recomputed from the data.** A percentile or a live median would move every
  hub's score when other hubs' data change (or hubs are added), which would trigger false
  risk-change alerts. The reference lives in the rules, so changing it is deliberate and creates a
  new rule set and scoring run (section 17).
- **Tiers on the index, fixed round bounds**: low < 50, moderate 50-99, elevated 100-149,
  high 150-199, very high >= 200. On the current data: 7 / 18 / 13 / 8 / 4 hubs (very high:
  Boston 270, Minneapolis 242, Denver 213, Milwaukee 212). Bounds are validated (start at 0, increasing).
- **Rankings are unchanged**: the index is proportional to days, so the order is the same.
- **Per month, season and set of months (pro-rated reference)**: a period is compared with a typical
  hub over the same length of time, i.e. reference x months / 12. A month is compared with a typical
  hub's average month (4.85 / 12 = 0.40 days): Denver March, 2.52 days -> index 624, very high.
  Rejected: comparing a month with a whole year (every month looks "low", tiers meaningless) and
  with the typical hub's same month (summer medians near 0 explode, like heat). With pro-rating,
  the 12 monthly indices (and the 4 seasonal ones) **average exactly to the annual index**, and the
  same tiers mean the same thing at every grain. Applies to `hub_breakdown` (month / season; a single
  calendar year uses the full reference), `rank_hubs` and `compare_hubs` with a season or months.
- **`monthly_scores` table**: hub x hazard (+ total) x month per scoring run (4,200 rows): days per
  year in that month, index, tier. Queryable in SQL, and the grain the scalable design uses.
- **Storage**: `hub_results` and `hazard_scores` gained `index_value` and `tier`. Scoring tables
  are derived, so a schema version (`PRAGMA user_version`) drops and rebuilds them on upgrade;
  source tables and chat history are untouched.
- **Shown in**: every tool result, `/rankings`, `/hubs/{id}`, the ranking table (Index, Tier columns)
  and the hub profile / comparison tables. The prompt asks the model to lead with days and may add
  index and tier ("6.08 days/yr, index 125, elevated").

---

## 19. When results are computed: on write, not on startup or per request

Three kinds of work, three moments (measured on the current data):

| Work | Cost | When | Why |
|---|---|---|---|
| Scoring: rules applied to the data -> disruption days, hazard / hub / monthly scores | 1.1 s for 50 hubs (minutes at 10,000) | **when the inputs change** (write path), stored as a scoring run | depends only on data + rules; per-request recompute repeats identical work; one stored run = every user and replica sees the same numbers; alerts compare stored runs |
| Views: rankings by season, comparisons, breakdowns | 0.6-6 ms from stored results | **on demand** | too many combinations to precompute; each is a sum of at most 12 monthly rows |
| Raw weather facts (`weather_stat`) | a few ms per hub | **on demand** | free-form conditions cannot be precomputed |

**Implemented:** ingestion now ends by creating a scoring run (`app/ingest.py::score_after_ingest`):
- a new run only if the data or rules actually changed (the snapshot fingerprint excludes the download log);
- **never after a failed ingestion**: incomplete data must not become the active scores; the previous run stays;
- it prints what moved compared with the previous active run (the comparison alerts will use);
- `--no-score` skips it. The server picks up the new run within 5 s (section 17), without a restart.

The startup check (score if the active run does not match the rules and data) stays as a safety
net, e.g. for a fresh clone or after editing the rules. Per-hub lazy scoring on demand would be
the right choice only with thousands of hubs that are rarely asked about.
