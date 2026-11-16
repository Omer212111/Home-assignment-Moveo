# Handoff: state of the project for a new AI session

Last updated: 2026-11-16 07:20 UTC. Work started around 13:30 UTC on 2026-11-15 (24-hour deadline).

## 1. The assignment (FDE take-home)
Build a "Weather Risk Intelligence Agent" for a US logistics company that ranks distribution hubs by
weather-disruption exposure to prioritise resilience investment. Required: public weather/hazard
APIs; deterministic ranking logic (not only LLM); clear reasoning; conversational follow-ups; a chat
interface that uses an API; a JSON-schema contract between the LLM and code; a small eval set with a
way to run it; clear assumptions and uncertainty. Deliverables: source code + run instructions (a
deployed URL is preferred), a short design/architecture doc (architecture, repository structure,
data storage choice), and **the full AI session transcript (mandatory)**. Bonus: voice; an alert when
a hub's score changes.

All four example questions are answered and covered by eval cases: Midwest winter (`rank_hubs`),
Miami vs Houston hurricane + flood (`compare_hubs`), Denver snowfall last year (`weather_stat`),
why Dallas is high (`explain_hub`, which states Dallas is rank 17 of 50).

## 2. Working agreement with the user (important)
- **Stop after every stage** so the user can review it. Explain the engineering dilemmas and why each
  choice was made. Do not start the next stage without approval.
- The user works on **Windows, PowerShell 5.1**: `&&` does not work, so give one command per code
  block. They use `uv`.
- **Anything that calls the Anthropic API or Open-Meteo runs on the user's laptop** (their `.env`
  holds `ANTHROPIC_API_KEY`). Give exact commands and ask for the output.
- Docs are in English. Commit and push directly to `main` after each change.
- **The UI targets desktop only** (user's decision).
- **The README must not mention the raw API cache** (user's decision); it is documented in
  `docs/DATA_MODEL.md` and `docs/DESIGN_DECISIONS.md` only.

## 3. Cloud-session constraints (found by testing)
- Reachable: OpenFEMA, NWS, PyPI/npm, GitHub (push works; `gh` works for Actions status).
- **Not reachable:** Open-Meteo (quota), FEMA National Risk Index, `onrender.com` (the live app),
  and the Anthropic API (TLS interception; API calls run on the laptop).
- Docker works after starting `dockerd`; builds need the sandbox CA injected through a temporary
  Dockerfile copy (`--build-context ca=/root/.ccr`); the committed `Dockerfile` stays clean.
- Browser checks: headless Chromium (Playwright) with `/chat` intercepted. Diagrams: mermaid-cli
  (`mmdc`) with Chromium at `/opt/pw-browsers/chromium`.

## 4. What is built (all on `main`)
| Area | Files | Notes |
|---|---|---|
| Hubs | `config/hubs.yaml` | 50 most populous US cities: lat/lon, county FIPS, Census region |
| Ingestion | `app/ingest.py`, `app/sources/*` | Open-Meteo daily 2021-2025 (91,300 rows), OpenFEMA declarations 2006-2026 (669 rows, 481 disasters, 47 counties); local raw cache (files or Redis, not in git); writes `data/snapshot/*.parquet` (committed) and ends with a scoring run (`DESIGN_DECISIONS.md` 16, 19, 22) |
| Scoring | `config/disruption.yaml`, `app/scoring/disruption.py`, `periods.py` | KPI expected disruption days per year; weather rules + FEMA DR declarations, de-duplicated; month / season / year breakdowns that sum to the annual total |
| Index | `config/disruption.yaml` `index` | 100 = median hub (4.85 days, fixed); tiers low < 50 ... very high >= 200; pro-rated per month and season (section 18) |
| Scoring runs | `app/scoring/store.py`, `service.py` | SQLite tables per run (rules hash + data fingerprint); one active; memory caches it and reloads a new run within 5 s (section 17) |
| API | `app/main.py` | `/` (page), `/health`, `/health/llm`, `/rankings`, `/hubs/{id}`, `/alerts`, `POST /alerts/check`, `POST /chat`; optional `APP_PASSWORD` |
| Agent | `app/agent/*` | Claude Sonnet 5.5, effort low; 5 tools (`rank_hubs`, `explain_hub`, `compare_hubs`, `hub_breakdown`, `weather_stat`); strict schemas; `ModelAnswer`; server attaches the ranking; at most 2 tool rounds (error rounds free); prompt caching; trajectories (sections 1-4, 9, 11-15) |
| UI | `app/static/index.html` | chat, details panel (ranking with index and tier, comparison, breakdown, weather tables), agent steps, alerts |
| Alerts | `app/alerts.py`, `config/alerts.yaml` | monthly outlook (index >= 300 and >= 3 events, 7 days ahead, one digest) + score change (>= 25 points or tier); dedup key, outbox with leases, safe under concurrency (sections 20-21) |
| Evals | `evals/` | 24 cases / 29 turns; correctness vs efficiency checks; `--case a,b`, `--repeat N`; history in `evals/history/` and comparison |
| Tests | `tests/`, `.github/workflows/tests.yml` | 92 offline tests; CI on every push (passing) |
| Deploy | `Dockerfile`, `render.yaml` | **live** at https://hub-weather-risk.onrender.com/ (password protected; free plan) |
| Docs | `README.md`, `docs/*`, `docs/DESIGN_SUMMARY.pdf` | one-page design summary PDF; architecture, data model, scalable design, decisions, evals |

Current top of the ranking: Boston 13.1 (index 270), Minneapolis 11.7, Denver 10.3, Milwaukee 10.3
(all very high, winter-driven). Virginia Beach (#6) is inflated by coastal wind (documented).

## 5. Run it
`uv sync`, `.env` with `ANTHROPIC_API_KEY`, `uv run uvicorn app.main:app --reload`, open
http://127.0.0.1:8000/. Tests: `uv run pytest -q`. Evals: `uv run python -m evals.run`.

## 6. Documentation to read first
- `README.md`: overview, run and deploy, KPI, agent, API, evals, data, architecture, storage, structure
- `docs/DESIGN_DECISIONS.md`: every decision with options and reasons (sections 1-22, chronological)
- `docs/ARCHITECTURE.md`: components, chat sequence, background flows, component table
- `docs/DATA_MODEL.md` / `docs/DATA_MODEL_SCALABLE.md`: tables today / target design at scale
- `docs/EVALS.md`: eval cases, scoring, and the analysis of runs 1-8
- `docs/QUESTION_CATALOG.md`, `docs/DATA_NOTES.md`, `docs/IMPROVEMENTS.md`

## 7. Eval history (details in `docs/EVALS.md`)
Runs 1-3: grounding fix, server-attached ranking, repeated calls. Run 4: loops until the step limit
-> forced-answer guardrail. Run 5: 58/58. Run 6: 110/111, repeats in 14/17 turns -> two-round limit.
Run 7: 110/111, prompt caching halves cost ($0.55). Run 8: 109/111, both failures were check
artifacts -> checks fixed (any matching call; ranking vs expected arguments). **Run 9 (24 cases, new
scoring) is pending on the laptop.**

## 8. Next steps (in order; stop for review after each)
1. **Session transcripts (mandatory deliverable)**: export the transcript of every AI session and
   commit it under `docs/ai-session/` with a short index (plan, sessions, outcomes).
2. **Eval run 9** on the laptop (`uv run python -m evals.run`), analyse it, record it in `docs/EVALS.md`.
3. Optional: voice input (Web Speech API in the page); live NWS alerts; uncertainty ranges per hub
   (`docs/IMPROVEMENTS.md`).
