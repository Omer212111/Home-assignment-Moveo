# Weather Risk Intelligence Agent: Architecture & Implementation Plan

> **Historical document.** This is the plan written before the build, kept as part of the record of
> the work. What was actually built differs in places (e.g. 50 hubs, five agent tools, scoring runs,
> alerts, deployment); for the current state see `README.md`, `docs/ARCHITECTURE.md` and
> `docs/DESIGN_DECISIONS.md`.

> Status: plan v1, written before any code. Every decision below comes with the
> engineering reason behind it, so the plan can also serve as the basis for the
> design document that has to be submitted.

---

## 0. Guiding constraints

| Constraint | What it means for the design |
|---|---|
| **24h wall clock, ~10–12 focused hours** | Keep the number of moving parts small: one language, one process, one deployable, one DB file. Use no framework whose behaviour I can't explain line by line. |
| **"Narrow scope done well"** | About 20 hubs, 5 hazards and one well-defined KPI. No RAG, no multi-agent setup, no auth. |
| **Must run live in the interview** | The demo must not depend on public APIs being up. Data gets ingested ahead of time into a committed snapshot, and live calls are an extra on top. |
| **Deterministic scoring, not LLM output** | The LLM never produces a number. It picks tools and writes the narrative. All numbers come from Python. |
| **Structured JSON between LLM and code** | Pydantic models are the single source of truth. They generate the JSON schemas for tool inputs and the final answer, and they also validate both. |
| **Mandatory: full AI session export** | The work is driven from this session, with checkpoints committed, so the transcript tells the story. |

**Build order principle:** every phase ends in a state I could demo. If time runs out, I cut from the end of the list. Nothing half-built sits in the middle.

---

## 1. System architecture

```
                         ┌──────────────────────────────────────────────┐
                         │                FastAPI service               │
 Browser                 │                                              │
 ┌──────────────┐  HTTP  │  /chat ──► Agent loop (Claude tool use)      │
 │ Chat UI      │───────►│              │  ▲                            │
 │ (static HTML │◄───────│              ▼  │ tool results (JSON)        │
 │  + JS, voice)│  JSON  │          Tool layer (pure Python functions)  │
 └──────────────┘        │              │                               │
                         │              ▼                               │
                         │   Scoring engine (deterministic, config-     │
                         │   driven weights) ◄── metrics ◄── SQLite     │
                         │                                    ▲         │
                         │  /hubs /rankings /alerts           │         │
                         │                                    │         │
                         │  Scheduler (APScheduler, hourly) ──┤         │
                         │      └─► recompute live score ─► Δ? ─► Webhook│
                         └────────────────────────────────────┼─────────┘
                                                              │
                     Ingestion (CLI `make ingest`, also at startup if DB empty)
                                                              │
        ┌──────────────────┬──────────────────┬───────────────┴────────┐
        ▼                  ▼                  ▼                        ▼
  Open-Meteo Archive   FEMA National     OpenFEMA Disaster      NWS api.weather.gov
  (daily obs, 5 yrs)   Risk Index (NRI)  Declarations           (active alerts, live)
```

### How the components talk to each other

1. **UI → API.** The UI sends `POST /chat {session_id, message}`. It gets back a validated `AgentAnswer` JSON, and renders the markdown answer, a ranking table and a "sources & assumptions" panel.
2. **API → Agent.** The agent loads the session history from SQLite and calls Claude with the tool definitions. It runs tool calls until the model returns its final structured answer, then validates that answer with Pydantic and saves the turn.
3. **Agent → Tools → Scoring → DB.** Tools are plain Python functions. They read pre-computed metrics and scores from SQLite. One tool can call Open-Meteo live for ad-hoc date ranges, such as "Denver last year".
4. **Scheduler → Alerts.** Every hour the scheduler pulls NWS alerts plus a 7-day forecast. It recomputes each hub's *operational* score and compares it with the last stored value. On a tier change or a large jump it writes an alert row and POSTs to a webhook.

---

## 2. Component-by-component decisions and reasoning

### 2.1 Language & web framework: **Python 3.12 + FastAPI + Pydantic v2**
- Python is the preferred language and has the best data tooling (pandas for metric aggregation).
- I chose FastAPI over Flask because **Pydantic is native to it**. The same models validate API requests, tool inputs and LLM output, and they generate the JSON schemas. One schema system for the whole app is the best way to meet the "enforce structured output" requirement with little code. FastAPI also gives free OpenAPI docs at `/docs`, which is handy for showing that the agent is exposed through an API.
- `uv` for dependency management, because it is fast and reproducible (`uv.lock`).

### 2.2 Data sources (public, free, no API keys)

| Source | Used for | Why this one |
|---|---|---|
| **Open-Meteo Historical (archive-api.open-meteo.com)** | Daily snowfall, precipitation, min/max temperature and max wind gust per hub, last 5 full years | No key. One call per hub returns years of daily data. Lat/lon based. It directly answers "% of days in Denver last year with snowfall". |
| **FEMA National Risk Index (county level)** | Hazard risk scores for hurricane, riverine and coastal flooding, winter weather, ice storm, tornado, heat wave | Authoritative and nationally normalized. It is based on long-run modelled loss. **This fixes the main weakness of station data:** 5 years of observations is far too short to measure hurricane exposure, a rare event. |
| **OpenFEMA Disaster Declarations API** | Number of federal declarations per county by incident type over 20 years | Simple and explainable proof that a hazard actually disrupted the area. Good material for "why is Dallas high?" |
| **NWS api.weather.gov** | Active alerts and forecast per point (live) | No key, official. Used only for the operational and alert score, not for the investment ranking. |

**Explicitly out of scope:** NOAA Storm Events bulk files (heavy to parse, low marginal value in 24h) and paid APIs.

### 2.3 Hub list: **a seeded `hubs.yaml` (~20 hubs)**
- The company gave us no hub list, so I define one. This is **assumption #1** and it is documented. Each entry has: id, city, state, lat/lon, county FIPS (to join NRI and FEMA data) and region (Census region, so "Midwest" has a precise meaning).
- The coverage is chosen so that every example question can be answered: about 8 Midwest hubs (Chicago, Minneapolis, Detroit, Indianapolis, Columbus, Kansas City, St. Louis, Milwaukee, Omaha), plus Miami, Houston, Dallas, Denver, Atlanta, Memphis, New Orleans, Phoenix, Los Angeles, Seattle and Newark.

### 2.4 Data storage: **SQLite (one file) + committed snapshot**
**Why SQLite:**
- The data is small: 20 hubs × ~1,826 days ≈ 37k daily rows, plus a few hundred rows of hazard and score data.
- It is relational and needs joins (hubs ⨝ metrics ⨝ NRI ⨝ score history).
- Zero ops. It ships with Python, needs no extra container and is trivial to deploy.
- It keeps **score history**, which the change-detection alert needs. Plain CSV or in-memory data can't do that cleanly.

**Why not the alternatives:**
- **Postgres** adds a service to run and deploy with no benefit at this scale. The data access layer will be plain SQL through a thin module, so moving to Postgres later is mechanical.
- **Vector DB / RAG** doesn't fit: there is no unstructured corpus. The questions are about numbers, and numbers belong in tables with deterministic code.
- **Only live API calls (no storage)** would make the demo slow and fragile and the results non-reproducible. Evals need stable data.

**Tables:** `hubs`, `daily_weather`, `hazard_index` (NRI), `disaster_declarations`, `hub_metrics` (derived), `hub_scores` (versioned, with timestamp), `alerts`, `conversations`.

**Snapshot:** after ingestion, `data/snapshot/*.parquet` gets committed. On startup the app builds the DB from the snapshot if it's missing. Live ingestion is optional (`make ingest`). *Reason: the interview demo can't depend on third-party uptime, and the evals need fixed ground truth.*

### 2.5 Scoring engine: **deterministic, transparent, config-driven**
This is the core of the "defined logic or KPI" requirement. I want it simple enough to explain on a whiteboard.

**Two separate scores.** Mixing them would mislead the user:
1. **Structural Exposure Score (0–100)** answers *"which hubs should we invest in?"* It changes slowly and is based on climatology plus NRI.
2. **Operational Risk Score (0–100)** answers *"is this hub at risk this week?"* It is based on NWS alerts and the forecast, and drives the alerting bonus.

*Reason:* an investment decision shouldn't swing because it's snowing today, and an alert should fire on current conditions. Keeping the two apart is a deliberate design point to discuss in the interview.

**Exposure score structure:**
```
Hazard sub-score (0–100) for each of: winter, hurricane_wind, flood, severe_convective, heat
  = weighted blend of
      observed component   (Open-Meteo metrics, e.g. snow-day %, heavy-snow days, freeze days)
      modelled component   (FEMA NRI hazard risk score, already a 0–100 national percentile)
      historical component (FEMA declarations of that type, 20 yrs)

Composite exposure = Σ hazard_weight[h] × subscore[h]        (weights in config/scoring.yaml)
Tier: Low <40 ≤ Moderate <60 ≤ High <80 ≤ Severe
```

**Normalization choice: fixed absolute caps, not ranks across hubs.** For example, snow on 30% or more of days maps to 100.
- *Reason:* scores stay stable when hubs are added or removed, and they are comparable over time. Both matter for the change alerts. A min-max scale across the hub set would make Dallas's score change just because a new hub was added. NRI is already a national percentile, so it is stable.

**Every score is returned with a breakdown:** each input's raw value, its normalized value, its weight and its contribution. That breakdown is what makes "**why** is Dallas high?" answerable *from data*. The LLM narrates the top contributors and doesn't invent reasons.

**Weights live in YAML** because the hazard mix is a business decision, not an engineering one. An analyst could reweight without touching code.

### 2.6 Agent: **hand-written tool-use loop on the Anthropic Python SDK (Claude)**
**Why a manual loop (about 100 lines) and not LangChain or LangGraph:**
- The loop is simple: call the model, run the requested tools, repeat until a final answer.
- A thin loop is fully transparent and easy to debug, with no hidden prompts. I can explain every line in the interview.
- It gives me direct control over logging each tool call into the trace, which the evals use. Frameworks add abstraction and version churn, which is risky under a 24h limit.

**Model:** `claude-opus-5-5` by default, with `effort: "low"` or `"medium"` for chat latency. The model is configurable through the `LLM_MODEL` env var. `claude-sonnet-5-5` is a cheaper and faster alternative if latency during the demo becomes a problem. That trade-off is your call.

**Tools.** Each one is small and does a single job. Inputs are Pydantic models exported as JSON schema with `strict: true`, so arguments always validate:

| Tool | Answers |
|---|---|
| `list_hubs(region?)` | Resolves "Midwest" and fuzzy city names to hub ids |
| `rank_hubs(hazard, region?, top_n)` | "Which Midwest hubs are most exposed to winter disruption?" |
| `compare_hubs(hub_ids, hazards)` | "Compare Miami and Houston for hurricane and flood" |
| `explain_hub_score(hub_id)` | "Why is Dallas's risk high?" (returns the contribution breakdown) |
| `weather_stat(hub_id, metric, start_date, end_date, threshold?)` | "% of days in Denver last year with snowfall" (DB first, live Open-Meteo for other ranges) |
| `active_alerts(hub_id)` | Live NWS context and the operational score |

**Structured output (the JSON-schema requirement):**
- The final answer is constrained with Claude **structured outputs** (`output_config.format` with a JSON schema generated from the Pydantic `AgentAnswer` model). It is then validated again with Pydantic. If validation fails, the agent retries once with the validation error, and otherwise returns a safe error object.
- Tool arguments use `strict: true`, so both directions of the LLM↔code boundary are schema-enforced.

```python
class AgentAnswer(BaseModel):
    answer_markdown: str
    hubs_referenced: list[str]                # hub ids
    ranking: list[RankedHub] | None           # {hub_id, score, tier, rank}
    key_drivers: list[Driver]                 # {hub_id, factor, value, contribution}
    data_sources: list[SourceRef]             # {name, period, url}
    assumptions: list[str]
    confidence: Literal["low", "medium", "high"]
    uncertainty_notes: str
    suggested_follow_ups: list[str]
```
*Reason:* the UI renders tables from `ranking`/`key_drivers` without parsing prose, and evals assert on fields instead of regex over text. `assumptions`, `confidence` and `uncertainty_notes` cover the "communicate assumptions, uncertainty and scoping" requirement **in every answer**, not only in the README.

**Grounding rule (in the system prompt, checked by the eval):** every number in the answer must appear in a tool result in this turn. If the data can't answer a question, the agent says so and doesn't guess.

**Conversation memory:** the message history is stored per `session_id` in SQLite and sent again on each turn. Conversations are short and tool results are compact, so there's no need for summarization or vector memory. Follow-ups like "and what about Chicago?" resolve naturally from the history. The history is trimmed to the last N turns as a safety cap.

### 2.7 API: **FastAPI endpoints**
```
POST /chat                     {session_id?, message} → AgentAnswer (+ tool trace if debug=true)
GET  /hubs                     hub list
GET  /hubs/{id}/score          exposure + operational score with breakdown
GET  /rankings?hazard=&region= deterministic ranking (no LLM)
GET  /alerts                   recent score-change alerts
POST /admin/refresh            run the operational re-score now (demo trigger for alerts)
GET  /health
```
*Reason:* the non-chat endpoints show that the scoring stands on its own without the LLM, and they are useful for the evals and the demo.

Streaming (SSE) is a **stretch goal** only. A non-streaming request with a "thinking…" indicator is fine for a demo, and streaming plus structured output adds complexity.

### 2.8 Chat UI: **one static HTML/JS page served by FastAPI**
**Why not Streamlit:** Streamlit would run as a second process, and deploying two services adds risk. It also makes voice awkward. One static page served by the same FastAPI app means **one container and one URL**. The page renders the chat, a ranking table, a score-breakdown bar and a collapsible "Sources / assumptions / confidence" panel from the JSON.

**Voice bonus (about 30 min):** the browser Web Speech API (`SpeechRecognition` for input, `speechSynthesis` to read the answer aloud). It needs no backend changes and no extra API keys. It works in Chrome, which is fine for a demo.

### 2.9 Alerts (bonus): **APScheduler in-process + generic webhook**
- An hourly job fetches NWS alerts and the forecast, recomputes the operational score and compares it with the last row in `hub_scores`.
- It fires an alert if the **tier changes** or **|Δ| ≥ 15 points**. *Reason:* without this hysteresis rule, small noise in the forecast would spam alerts.
- Each alert is written to the `alerts` table, shown in the UI and POSTed to `ALERT_WEBHOOK_URL` (a Slack incoming webhook, or webhook.site for the demo).
- `POST /admin/refresh` lets me trigger the job live in the interview.
- *Why in-process:* no extra infrastructure. The limitation is documented: with multiple replicas, the job should move to an external cron, such as a platform cron or GitHub Actions calling `/admin/refresh`.

### 2.10 Evaluation: **two layers, runnable with one command**
1. **Unit tests (pytest, no LLM, fast and free):** scoring math, normalization caps, tier boundaries, weather-stat calculations against hand-checked fixtures.
2. **Agent eval set (`evals/cases.yaml`, ~15–20 cases)**, run with `make eval`. It prints a pass/fail table and writes `evals/report.json`.

| Check | How |
|---|---|
| Schema validity | The answer parses as `AgentAnswer` |
| Tool selection | The expected tool was called (from the trace) |
| Entity correctness | `hubs_referenced` ⊇ expected hubs (e.g. Midwest question → only Midwest hubs) |
| Ranking fidelity | The LLM's ranking order equals the deterministic `rank_hubs` output |
| Numeric accuracy | The Denver snow-day % is within ±0.5pp of the ground truth computed straight from the DB |
| Grounding | Every number in the answer appears in the tool outputs |
| Scoping and refusal | "Hub in Paris?" or "weather in 2035?" → states it's out of scope, `confidence=low` |
| Follow-ups | Multi-turn cases ("…and Chicago?") resolve to the right hub |

The cases cover the 4 example questions, plus paraphrases, follow-ups and out-of-scope prompts. An LLM-as-judge for explanation quality is a stretch goal. Deterministic checks come first because they're cheap and reliable.

### 2.11 Deployment: **Docker → Render or Fly.io (one container)**
- A Dockerfile builds the app, and the DB is built from the committed snapshot at startup. The only secret is `ANTHROPIC_API_KEY` as an env var.
- *Reason:* one container from a snapshot means the deploy is stateless and survives restarts on free tiers with ephemeral disks. Alert history is lost on restart, which is acceptable and documented. A Fly volume would fix it.
- Fallback: `make run` locally. **I'll test the local path first**, because the deploy is "preferred, not mandatory".

---

## 3. Repository structure

```
weather-risk-agent/
├── README.md                 # run instructions, demo script, assumptions & limits
├── docs/
│   ├── ARCHITECTURE.md       # the required design doc (derived from this plan)
│   ├── PLAN.md               # this file
│   └── ai-session/           # exported AI agent session (mandatory deliverable)
├── config/
│   ├── hubs.yaml             # seeded hub list (assumption)
│   └── scoring.yaml          # hazard weights, normalization caps, tier cut-offs
├── app/
│   ├── main.py               # FastAPI app, routes, static UI, scheduler start
│   ├── settings.py           # env config (model, webhook URL, DB path)
│   ├── db.py                 # SQLite schema + query helpers
│   ├── sources/              # one module per public API (httpx + retries + cache)
│   │   ├── open_meteo.py
│   │   ├── fema_nri.py
│   │   ├── fema_declarations.py
│   │   └── nws.py
│   ├── ingest.py             # pulls sources → DB → snapshot
│   ├── scoring/
│   │   ├── metrics.py        # daily weather → per-hub metrics
│   │   └── engine.py         # sub-scores, composite, breakdown, tiers
│   ├── agent/
│   │   ├── schemas.py        # Pydantic: tool inputs, AgentAnswer
│   │   ├── tools.py          # tool implementations + registry
│   │   ├── prompts.py        # system prompt (grounding, scoping rules)
│   │   └── loop.py           # Claude tool-use loop, validation, retry
│   ├── alerts/
│   │   ├── monitor.py        # operational re-score + change detection
│   │   └── notify.py         # webhook POST
│   └── static/index.html     # chat UI (+ voice)
├── data/snapshot/            # committed parquet snapshot for reproducible demo
├── evals/
│   ├── cases.yaml
│   └── run.py
├── tests/                    # pytest: scoring, metrics, schemas
├── Dockerfile
├── Makefile                  # setup / ingest / run / test / eval
├── pyproject.toml / uv.lock
└── .env.example
```
*Reason:* the folders follow the architecture diagram one to one (sources → scoring → agent → API), so a reviewer can map the doc to the code at a glance. The scoring code has no LLM imports and the agent code has no HTTP-to-weather imports, so each layer can be tested on its own.

---

## 4. Time plan (24h window, ~12 working hours + buffer)

| Phase | Hours | Output | Demo-able checkpoint |
|---|---|---|---|
| **P0: Scaffold** | 0:00–0:45 | repo, uv, FastAPI hello, `hubs.yaml`, Makefile | `make run` serves `/health` |
| **P1: Data ingestion** | 0:45–2:45 | 4 source clients, SQLite schema, `make ingest`, snapshot committed | DB has 5 yrs × 20 hubs, NRI and declarations |
| **P2: Scoring engine + tests** | 2:45–4:45 | metrics, sub-scores, composite, breakdown, pytest | **M1:** `GET /rankings?hazard=winter&region=Midwest` returns a sensible ranking with no LLM |
| **P3: Agent + structured output** | 4:45–7:15 | tools, Pydantic schemas, loop, `/chat`, session memory | **M2:** all 4 example questions answered via curl |
| **P4: Chat UI (+ voice)** | 7:15–8:45 | static page, tables, sources panel; voice in the last 30 min | **M3:** end-to-end in the browser |
| **P5: Evals** | 8:45–10:00 | cases.yaml, runner, report | `make eval` shows a pass rate |
| **P6: Alerts (bonus)** | 10:00–11:00 | scheduler, change detection, webhook, `/admin/refresh` | the alert appears on webhook.site |
| **P7: Deploy + docs** | 11:00–12:30 | Docker, Render/Fly, README, ARCHITECTURE.md, session export | public URL |
| **Buffer** | 12:30+ | fixes, prompt tuning against the evals, rest before the interview | |

**What to cut, in order, if behind schedule:** voice, then deploy (run locally instead), then the alerts bonus, then live Open-Meteo fallback in `weather_stat`, then the flood sub-score detail. **Never cut:** deterministic scoring, structured output, evals, assumptions doc. Those are hard requirements.

---

## 5. Assumptions & known limitations (to be repeated in the README)

1. The hub list is invented. Real hubs would replace `hubs.yaml`.
2. **Exposure ≠ risk.** We measure hazard exposure only. True risk also needs vulnerability and consequence: the hub's throughput, revenue, building specs and backup routes. None of that was given. The weights config is where it would plug in.
3. Observations come from a gridded reanalysis (Open-Meteo), not the hub's own weather station. NRI and the FEMA declarations are county level, not site level.
4. 5 years of observations is a short window for rare hazards. That is why the hurricane and flood scores lean on NRI's long-run modelling. The confidence field reflects this.
5. Scoring weights and caps are expert judgment, not fitted to loss data. With historical disruption or cost data from the company, the weights could be calibrated.
6. "Last year" means the last full calendar year, and the agent states this explicitly.

---

## 6. Open decisions for you

1. **LLM provider and model.** The plan assumes you have an Anthropic API key (`claude-opus-5-5`, with Sonnet 5.5 as a latency fallback). If you'd rather use another provider, only `agent/loop.py` changes.
2. **Deploy target.** Render (simplest) or Fly.io (persistent volume for alert history). It can also stay local only.
3. **Hub list.** Use my seeded ~20 hubs, or do you have a preferred list?
