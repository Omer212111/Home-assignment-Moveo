# Improvements: what could come next

The baseline and its extensions are built (README section 12). This list is what would come next,
ordered roughly by value for the investment decision. Status as of 2026-11-16: sections 1-7 and 9
are open; section 8 is done except D; built since the list was written: standardised index and
tiers, scoring runs, alerts, time breakdowns, deployment (see `docs/DESIGN_DECISIONS.md` 14-22).

## 1. A cost metric instead of days only (research task)
The baseline KPI is expected disruption days per year. An investment committee thinks in money,
so the next step is **expected annual cost = downtime cost + property damage**. This needs
sources found and checked online. Candidates to evaluate (none verified yet unless marked):

| Need | Candidate sources to investigate |
|---|---|
| Property damage per hazard | **OpenFEMA Public Assistance Funded Projects** (damage repair $ per disaster per county; reachable from our environment, checked), FEMA National Risk Index Expected Annual Loss (modelled building loss per hazard and county), NOAA Storm Events Database (reported property damage per event and county), NOAA Billion-Dollar Weather and Climate Disasters (event costs by state), NFIP flood insurance claims (OpenFEMA; endpoint not found yet) |
| Cost of a lost or slowed operating day | Trucking cost-per-hour benchmarks (e.g. industry operational-cost studies), FHWA road weather statistics on weather-related delay, freight statistics from the Bureau of Transportation Statistics |
| Company-specific inputs (best source) | Revenue or throughput per hub-day, SLA penalties, the company's own downtime and claims history |

For each candidate, record: what it measures, geography and time coverage, licence, how often it updates, and how it would plug into the model. Then choose the cost formula with the analysts.

## 2. Tail risk next to the average
Add a **worst single event in 20 years** column (days lost in the worst event) next to the annual average, so hurricane hubs like Houston and Miami show their catastrophic exposure. Later: return periods (e.g. "1-in-10-year event costs X days").

## 3. Fix the coastal-wind overstatement
Coastal hubs (Virginia Beach, Oakland, Boston) get grid cells that partly cover open water. Options:
- check whether Open-Meteo can select a land-only grid cell for a coordinate;
- move the hub coordinate a few km inland, to where a distribution hub would realistically be;
- use gusts measured at the nearest airport weather station (NOAA station data) instead of the grid.

## 4. Calibrate rules with real downtime data
Severities, recovery days and the 7 days per hurricane are expert judgment. With the company's downtime logs, fit them (e.g. regression of actual lost hours on the trigger days). The config file is built so that this changes numbers, not code.

## 5. Uncertainty ranges
Run each hub with low / base / high parameter sets and report a range (e.g. "Minneapolis 9-14 days/yr"), and flag hubs whose rank changes between scenarios.
Cheaper first step with the data we have: leave one year out at a time (5 runs) and report the range
and whether the hub's rank and tier are stable. The scoring runs make the comparison a single command.

## 6. Longer and better weather history
- 10-20 years instead of 5 (needs a paid Open-Meteo key or several days of free quota) to reduce sampling noise.
- Check the snowfall bias against station data (NOAA daily station records) instead of assuming ~30%.

## 7. FEMA National Risk Index
Add modelled hurricane, flood and tornado frequency per county (rare events that 5 years of weather cannot capture). The download is blocked from our environment; it needs a manual CSV download.

## 8. Agent loop fixes found in eval run 3 (see `docs/EVALS.md`)
Run 3 showed that duplicate tool calls always arrive in a *later* model call, never in the same
response, so the per-question cache saves the tool work but not the extra round trip (~5k input
tokens, ~2 s). It also exposed a user-visible bug. Proposed fixes, in priority order:

| # | Fix | Why |
|---|---|---|
| A | **Attach the ranking deterministically** (`app/agent/loop.py:_ranking_from`): among `rank_hubs` calls with the same hazard and region, attach the one with the largest `top_n` (a top 10 is a prefix of the top 50); with different filters, the last call. | **Bug:** in `impossible_n` the model called `top_n=50`, then `top_n=10`; the server attaches the last call, so a user who asked for 100 hubs saw 10 in the table. Code-only fix, independent of the model. |
| B | **Fix the eval check** (`evals/checks.py:ranking_mismatch`): compare against the engine's ranking for the case's *expected* arguments, not the last call; add a `max_model_calls` expectation so extra round trips fail a check. | The check uses the same "last call" rule as the server, so it cannot catch bug A; duplicates are only printed today. |
| C | **Prompt:** "if the result has more than 10 rows, show its first 10; do not call the tool again for a shorter list". | The rule "show at most 10 rows" is the likely cause of the 50 -> 10 re-call. |
| D | **Smaller tool result for long rankings:** full detail for rows 1-10, only rank/city/days for rows 11-50 (the server keeps the full result for the UI). | `impossible_n` is the most expensive case (~29k input tokens); the 50 detailed rows are re-sent on every later model call. |
| E | **Prompt caching** on system prompt, tools and history. | Makes any extra round trip, and every follow-up, much cheaper. |

Status: **A done**, **E done** (run 7: cost halved), **B done** after run 8 (any matching call;
ranking vs the expected arguments; efficiency checks), C replaced by the tool-round limit
(`DESIGN_DECISIONS.md` section 15). **D open.** The tool-round question was settled with the user:
at most two tool rounds plus a retry after an error, not one round, so chained questions still work.

## 9. Live alerts and voice (bonus items not built)
- Live NWS alerts (`api.weather.gov`, reachable) for "a storm is coming this week", next to the
  historical monthly outlook; short-lived data, a natural use of Redis with a TTL.
- Voice input in the page with the browser's Web Speech API (no server change).

## 10. Smaller items
- Group neighbouring hubs into metros (Dallas / Fort Worth / Arlington, the Bay Area, Phoenix / Mesa, LA / Long Beach).
- Statewide FEMA declarations (county code 000) are ignored today; decide whether to include them.
- Exposure is not risk: add hub vulnerability (building standard, backup power, alternative routes) when the company can provide it.
