# Handoff: state of the project for a new AI session

Last updated: 2026-11-15 17:30 UTC, after commit "Docs: design decisions for the chat UI".

## 1. The assignment (FDE take-home, 24-hour deadline)
Build a "Weather Risk Intelligence Agent" for a US logistics company that ranks distribution hubs by weather-disruption exposure to prioritise resilience investment. Required:
- public weather/hazard APIs
- deterministic ranking logic (not only LLM)
- clear reasoning
- conversational follow-ups
- a chat interface that uses an API
- a JSON-schema contract between the LLM and code
- a small eval set with a way to run it
- clear assumptions and uncertainty
- deliverables: source code + run instructions (a deployed URL is preferred), a short design/architecture doc (architecture, repository structure, data storage choice), and **the full AI session transcript (mandatory)**
- bonus: voice; an alert when a hub's score changes

Example questions the agent should answer:
1. Which hubs in the Midwest are most exposed to winter disruption? (**done**)
2. Compare Miami and Houston in terms of hurricane and flood exposure. (**done, slice 3**)
3. What percentage of days in Denver last year had snowfall? (**done, `weather_stat`**)
4. Why is the Dallas hub's weather disruption risk high? (**done, slice 3**)

Work started around 13:30 UTC on 2026-11-15; about 4 hours used so far.

## 2. Working agreement with the user (important)
- **Stop after every stage** so the user can review it. Explain the engineering dilemmas and why each choice was made. Do not start the next stage without approval.
- The user works on **Windows, PowerShell 5.1**: `&&` does not work, so give one command per code block. They use `uv`.
- **Anything that calls the Anthropic API or Open-Meteo runs on the user's laptop** (their `.env` holds `ANTHROPIC_API_KEY`). Give them exact commands and ask them to paste the output.
- When the user asks, save explanations into `docs/` (e.g. `docs/DESIGN_DECISIONS.md`). Docs are in English.
- **The UI targets desktop only** (user's decision): test at desktop size; no phone work. Existing small-screen CSS rules stay but are not maintained.
- Commit and push directly to `main` on https://github.com/Omer212111/home-assignment-moveo after each change.

## 3. Cloud-session constraints (found by testing)
- Reachable: OpenFEMA (`www.fema.gov`), NWS (`api.weather.gov`), PyPI/npm.
- **Not usable:**
  - Open-Meteo (shared-IP quota exhausted; other Open-Meteo hosts blocked)
  - FEMA National Risk Index (ArcGIS host blocked; the static CSV URL redirects)
  - cdn.jsdelivr.net
  - **the Anthropic API**: the credential form refuses Anthropic hosts, and the session does not sign requests (401). The user chose to run API calls on the laptop.
- A personal cloud environment named `home-assignment-moveo` exists (same network as the Default environment).
- For browser checks: headless Chromium via Playwright, with `/chat` intercepted (see `docs/DESIGN_DECISIONS.md` section 10). Mermaid can be installed from npm to render diagrams.

## 4. What is built (all on `main`)
| Area | Files | Notes |
|---|---|---|
| Hubs | `config/hubs.yaml` | 50 most populous US cities: lat/lon, county FIPS, Census region |
| Ingestion | `app/ingest.py`, `app/sources/*` | Open-Meteo daily 2021-2025 (91,300 rows, run on the laptop), OpenFEMA declarations 2006-2026 (669 disasters, 47 counties); raw cache `data/raw/`, snapshot `data/snapshot/*.parquet` (committed); SQLite rebuilt at startup |
| Scoring | `config/disruption.yaml`, `app/scoring/disruption.py` | **KPI: expected disruption days per year**; weather rules (worst rule per day, events with recovery tails), FEMA DR declarations for hurricanes (7 days each) and severe storms (1.5), de-duplication, calibration v2 (heat 43 C, cold -25 C, snow 7/17 cm, ice proxy) |
| Report CLI | `app/scoring/report.py` | `uv run python -m app.scoring.report [--hazard --region --hub]` |
| API | `app/main.py` | `GET /` (UI), `/health`, `/rankings`, `/hubs/{id}`, `POST /chat` |
| Agent | `app/agent/{schemas,tools,prompts,loop}.py` | `claude-sonnet-5-5`, effort low, refusal fallbacks; hand-written tool loop; tools `rank_hubs`, `explain_hub`, `compare_hubs` (slice 3, `DESIGN_DECISIONS.md` section 11); `ModelAnswer` = LLM output schema (no ranking), `AgentAnswer` = API response (server attaches the ranking from the engine); per-question cache for identical tool calls; tool schemas via `anthropic.transform_schema` (strict mode rejects min/max) |
| UI | `app/static/index.html` | single page, no libraries, chat + details panel |
| Raw cache | `app/sources/http_cache.py` | Redis when `REDIS_URL` is set (`import-files`, `status`), else `data/raw/` files (`DESIGN_DECISIONS.md` section 16) |
| Scores | `app/scoring/store.py` | SQLite scoring tables, one set per scoring run; memory caches the active run (`DESIGN_DECISIONS.md` section 17) |
| Tests | `tests/` | 62 offline tests (`uv run pytest -q`) |
| Evals | `evals/cases.yaml`, `evals/checks.py`, `evals/run.py` | 15 cases / 17 turns; `uv run python -m evals.run` (~$0.16-0.20 per run, laptop only) |

Current top of the ranking: Boston 13.1, Minneapolis 11.7, Denver 10.3, Milwaukee 10.3, Chicago 9.6 (winter-driven). Virginia Beach (#6) is inflated by coastal wind, a documented limitation.

## 4b. Run the chat locally
`uv run uvicorn app.main:app --reload`, then open http://127.0.0.1:8000/ (needs `ANTHROPIC_API_KEY` in `.env`).
Agent trajectories (thinking summaries, tool calls): "Agent steps" switch in the UI; files in `data/trajectories/`; `uv run python -m app.agent.trajectory` prints the latest (`DESIGN_DECISIONS.md` section 12).

## 5. Documentation to read first
- `docs/DESIGN_DECISIONS.md`: every decision for the API, agent and UI, with reasons (sections 1-10)
- `docs/ARCHITECTURE.md` (+ `docs/img/`): component diagram, chat sequence, component table
- `docs/DATA_NOTES.md`: data checks, calibration reasons, coastal-wind limitation
- `docs/EVALS.md`: eval cases, run 1 and run 2 results, changes made
- `docs/IMPROVEMENTS.md`: post-baseline list (cost metric research, tail risk, NRI, ...)
- `docs/PLAN.md`, `docs/PLAN-v2-slices.md`: the original plan

## 6. Eval history
- **Run 1:** 36/37. "5 years" was not grounded (the model derived it); a duplicate tool call in the baseline; a 50-hub answer took 22 s and 4k output tokens.
- Changes after run 1: `weather_years` in the tool's `method`; per-question cache; the server attaches the ranking; at most 10 rows in the answer text.
- **Run 2:** 37/37, $0.16; the 50-hub answer took 9.6 s and 1,147 output tokens. **But duplicate calls persist**: baseline and `impossible_n` each called `rank_hubs` twice with identical arguments (`impossible_n` input went up to 26,769 tokens). Report committed (`ba3f1a0`).
- Changes after run 2 (commit `e1a48c2`): the prompt no longer says "your last call"; each tool call records the `step` (model call) that requested it; the runner prints steps, cached duplicates and a duplicate count.
- **Run 3:** 36/37 (see `docs/EVALS.md`). Duplicates come in separate model calls; `impossible_n` exposed a ranking-attach bug. Fixes postponed to `docs/IMPROVEMENTS.md` section 8.

## 7. Next steps (in order; stop for review after each)
1. ~~Analyse eval run 3~~ (done; fixes A-E deferred to `docs/IMPROVEMENTS.md` section 8 by the user's choice).
2. **Slice 3 (built, eval run 4 pending on the laptop): `compare_hubs` and `explain_hub` tools** for "Compare Miami and Houston (hurricane + flood)" and "Why is Dallas high?". `explain_hub` can use `/hubs/{id}` data: per-hazard days, drivers, counted FEMA disasters. Add eval cases.
3. **Done (time-dimension slice, eval run 6 pending): `weather_stat`, `hub_breakdown`, `period` on rank/compare** (`docs/QUESTION_CATALOG.md`). Original note: **Slice 4: `weather_stat` tool** for "% of days in Denver last year with snowfall" ("last year" = 2025, inside the stored window; state the date range and the >= threshold used). Add eval cases.
4. **README rewrite**: run instructions (Windows + macOS/Linux), assumptions and limitations, repository structure, data storage choice (the required design-doc items).
5. **Deploy to Render**: Dockerfile; `ANTHROPIC_API_KEY` as a Render env var; the DB is rebuilt from the snapshot at startup.
6. Bonus, only if time allows: voice (Web Speech API in the page); alerts (NWS + scheduler + webhook).
7. **Session transcripts (mandatory deliverable)**: export the transcript of every AI session and commit it under `docs/ai-session/`.

If short on time, cut in this order: voice, alerts, deploy (run locally instead).
