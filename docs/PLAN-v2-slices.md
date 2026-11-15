# Weather Risk Intelligence Agent: Plan v2 (vertical slices, data first)

## Context
This is a take-home assignment for an FDE role, with a 24h deadline and about 12 hours of work. The full design reasoning is in `/home/user/PLAN.md`.

What you decided:
- **Build in thin end-to-end slices.** Each slice gets a simple demo, and the base grows only after a slice works end to end. Slice 1 is **"list the most dangerous hubs"**.
- **Hubs = the 40 most populous US cities.**
- LLM: Claude `claude-opus-5-5` (later changed to `claude-sonnet-5-5` to lower cost). Deploy target: Render.
- Focus now: **data** (which parameters to keep, how often to update them, whether to cache).

## Hubs (`config/hubs.yaml`): 50 most populous US cities (UPDATED)
This list replaces the 40-city list below, which stays for reference. The source is Census 2023 city population estimates; ranks around #48–52 are close calls, documented as an assumption.

The 40 cities below, plus 10 more:
- **South:** Raleigh, Virginia Beach, Miami, Tulsa, Tampa, Arlington TX
- **West:** Long Beach, Oakland, Bakersfield
- **Midwest:** Minneapolis

Effects:
- **Miami is now included**, so the special hub #41 is no longer needed.
- **Minneapolis adds a strong Midwest winter case.**
- **Final regional split:** Midwest 8, South 21, West 18, Northeast 3.
- **Hubs cluster around a few metros:** Dallas/Fort Worth/Arlington, SF/Oakland/San Jose, LA/Long Beach (the same county, 06037, so identical FEMA data), Phoenix/Mesa. Rankings may show these neighbours side by side. The agent mentions this, and grouping by metro is a possible later extension.
- **Open-Meteo cost:** 50 hubs × 5 years is roughly 5k counted calls, which fits one day's free quota from your laptop.

## Pydantic: yes, used at every boundary (no ORM)
| Where | Model | Why |
|---|---|---|
| Settings | `pydantic-settings` `Settings` (model, DB path, API keys from env) | config is typed and checked at startup |
| Config files | `Hub`, `ScoringConfig` loaded from YAML | a typo in `hubs.yaml` or `scoring.yaml` fails at startup, not mid-demo |
| External API responses | `OpenMeteoDaily`, `FemaDeclaration` | catches changes in the source's response format right at ingestion |
| HTTP API | `ChatRequest`, `RankingResponse`… | FastAPI validates them and generates `/docs` from them |
| LLM → code (tools) | one input model per tool, `extra="forbid"` → JSON schema with `strict: true` | tool arguments always validate |
| LLM → code (final answer) | `AgentAnswer` → `output_config.format`, validated again with `model_validate_json` | meets the JSON-schema requirement |
| Not used | DB rows / pandas frames inside scoring | plain SQL and DataFrames are simpler and faster |

Why Pydantic: **one source of truth.** The same class produces the JSON schema the LLM sees and checks what the LLM returns, so the two can't drift apart the way hand-written schemas do. It's native to FastAPI, and the Anthropic SDK accepts Pydantic models directly.

## Hubs (original 40-city list)
These are the 40 most populous US cities. Each hub stores: `hub_id, city, state, lat, lon, county_fips, census_region, pop_rank`. The location is the city centroid. For cities that span several counties, the county is the one containing the centroid. That choice is documented as an assumption.

- **Midwest (7):** Chicago, Columbus, Indianapolis, Detroit, Milwaukee, Kansas City, Omaha
- **South (15):** Houston, San Antonio, Dallas, Jacksonville, Austin, Fort Worth, Charlotte, Oklahoma City, Nashville, Washington DC, El Paso, Louisville, Memphis, Baltimore, Atlanta
- **West (15):** Los Angeles, Phoenix, San Diego, San Jose, San Francisco, Seattle, Denver, Las Vegas, Portland, Albuquerque, Tucson, Fresno, Sacramento, Mesa, Colorado Springs
- **Northeast (3):** New York, Philadelphia, Boston

⚠️ **Miami is not in the top 40** (it ranks around 44th), but one of the assignment's example questions is about Miami. Recommendation: add Miami as hub #41, flagged `added_for_example_question: true`.

## Data decisions

### 1. Parameters to keep
**Principle:** store raw **daily** facts, and derive metrics in code with thresholds in `config/scoring.yaml`. A new question or KPI then never needs a re-fetch. Hourly data would be about 24× the volume, and disruption is a per-day concept, so it's not worth keeping.

| Table | Fields | Why |
|---|---|---|
| `hubs` | above | join key to every source |
| `daily_weather` (Open-Meteo archive) | `hub_id, date, tmax, tmin, precip_sum, rain_sum, snowfall_sum, wind_gusts_max, wind_speed_max, weather_code` | Each field maps to a disruption mechanism: snow and ice close roads and yards, heavy rain causes flooding, gusts stop dock and trailer work, extreme heat limits labour and equipment. `weather_code` 56/57/66/67 = freezing rain, the ice signal. |
| `disaster_declarations` (OpenFEMA, ✅ verified reachable) | `disaster_number, incident_type, declaration_type, title, declaration_date, incident_begin_date, state_fips, county_fips` | Evidence that a hazard actually caused disruption. **Dedupe by disaster_number per county.** Keep only weather incident types; Biological, Earthquake and similar are excluded. |
| `hazard_index` (FEMA NRI, slice 2) | for each hazard (HRCN, RFLD, CFLD, WNTW, ISTM, TRND, HWAV, SWND): `_AFREQ`, `_EALS`, rating, `NRI_VER` | Long-run modelled exposure. Needed for rare hazards like hurricanes. Use **AFREQ/EALS, not RISKS**: the RISK score includes the community's social vulnerability, which describes residents rather than a warehouse. *Field names still need checking against the downloaded file.* |
| `hub_metrics` (derived) | per hub and year: snow_day_pct, heavy_snow_days (≥10cm), freeze_days, extreme_cold_days (≤−18°C), freezing_rain_days, heavy_precip_days (≥50mm), high_gust_days (≥75km/h), extreme_heat_days (≥38°C) | the inputs to the scores |
| `hub_scores` | `hub_id, hazard, score, tier, breakdown_json, scoring_version (hash of scoring.yaml), data_as_of, computed_at` | Reproducibility, explainability, and later change detection for alerts |
| `ingest_runs` | `source, params, started_at, rows, status, raw_file` | Provenance: answers "where did this number come from?" |

**History window: 5 full years (2021–2025).** Open-Meteo counts a long request as many API calls: roughly one per 14 days per 10 variables. So 5 years × 41 hubs costs about 4k of the 10k/day free quota, and 10 years would cost about 8k. Five years is enough for frequent hazards. NRI covers the rare ones.

**Found while checking the APIs:** Dallas County has 17 declarations since 2005, and **8 of them are "Hurricane"**. These are mostly evacuee-hosting declarations (Katrina, Rita, Harvey), not direct hits. So declarations measure *impact or involvement*, not physical exposure. Hurricane declarations for inland counties get a lower weight, and the agent states this caveat. This is a good interview talking point.

### 2. Update frequency
**Principle:** freshness should match the **decision cadence** (an annual investment choice) and each **source's own update cadence**. Refreshing more often than either adds cost and buys nothing.

| Data | Source updates | We refresh | Reason |
|---|---|---|---|
| hubs | manual | when changed | it's config |
| daily_weather | daily, about a 5-day lag (ERA5) | **monthly incremental append** (new days only) | a few new days barely move a 5-year climatology |
| declarations | continuously | **weekly** | cheap, and low volume |
| NRI | about once a year | **quarterly version check** | rarely changes |
| hub_scores | n/a | **after every ingest or `scoring.yaml` change** | the version hash keeps it reproducible |
| NWS alerts (alerts slice) | minutes | 15–60 min | operational, not strategic |

Slice 1 uses a one-time backfill plus a manual `make refresh`. Scheduled refresh arrives with the alerts slice.

### 3. Caching: yes, in 3 layers (no Redis)
1. **Raw HTTP response cache on disk** (`data/raw/<source>/<hash>.json`). Historical data never changes, so the cache never needs to expire. Reasons:
   - **The rate limit is real.** From this cloud container, Open-Meteo already answers *"Daily API request limit exceeded"* because the IP is shared with other users.
   - Re-running scoring during development must not re-fetch.
   - The raw files are an audit trail.
2. **SQLite as a materialized store, plus a committed snapshot.** The request path never calls an external API for history. Results: millisecond latency, the demo survives API outages, and evals stay deterministic.
3. **A short-TTL (about 10 min) in-process cache for live calls** such as NWS and ad-hoc ranges, so repeated chat questions don't hammer APIs. (Later: Anthropic prompt caching for the stable system prompt and tools.)

No Redis or Memcached: there is one process, the data is tiny, and scores are precomputed.

### Data access status (checked from this container)
- ✅ OpenFEMA: works
- ✅ NWS: works
- ❌ Open-Meteo: daily quota exhausted on the shared IP. **Fix:** run `make ingest` on your laptop and commit the snapshot, or retry after the daily reset.
- ❌ FEMA NRI ArcGIS host and NOAA ACIS: blocked by this environment's network policy. The NRI static CSV URL now redirects. NRI moves to slice 2, via a manual CSV download committed as a 41-county subset.

### The Open-Meteo fix: effect on the code and the interview
**Code: the code doesn't change. It only fixes *where* `make ingest` runs.**
- Ingestion (batch, offline) and serving (online) are already separate processes. The server reads only SQLite, which is rebuilt from `data/snapshot/` at startup. Wherever ingest runs (laptop, container, cron), the code is identical.
- Small additions:
  - an optional `OPEN_METEO_API_KEY` setting, which switches to the paid endpoint (production path, commercial licence)
  - `--hubs` and `--start/--end` flags on ingest, so a partial run is possible
  - the snapshot also carries `ingest_runs` and the raw JSON, for provenance
  - the snapshot is under 1 MB of parquet
- **Live fallback, the one real risk:** `weather_stat` for a date range outside the snapshot would call Open-Meteo from Render, whose IP may also be rate-limited. So the snapshot covers every range the demo needs ("last year" = 2025 is inside 2021–2025). The live fallback **degrades gracefully**: the agent says "outside stored window, live source unavailable" and sets `confidence=low`. It never crashes or makes up a number.

**Interview: present it as an architecture decision, not a workaround.**
- The talking point: *"Ingest offline, serve from a versioned snapshot."* That's how a real customer deployment works. Customer networks often restrict outbound traffic, exactly like the sandbox I built in.
- The story: I found it during the build (the shared-IP quota, and Open-Meteo counting long requests as many calls). The trade-off is freshness for reliability and reproducibility. That's acceptable because the decision is made once a year.
- **Proving the data is real:** show `ingest_runs` and the raw JSON. Optionally run `make ingest --hubs denver` live from the laptop (about 130 counted calls). The UI shows `data_as_of` with every answer.
- Production next steps to mention: a paid Open-Meteo key (the free tier is **non-commercial only**, a licensing point an FDE should raise) or NOAA as the source, and a scheduled monthly ingest job writing to a managed DB.

## Slice 1: "Which hubs are the most dangerous?" (end to end, ~4h)
1. Scaffold: uv, FastAPI, `Makefile`, `config/hubs.yaml` (41 hubs), `config/scoring.yaml`.
2. Data source modules:
   - `app/sources/open_meteo.py` and `app/sources/fema_declarations.py`: httpx, retries, raw disk cache.
   - `app/db.py` (schema above) and `app/ingest.py`: backfill, then export the snapshot to `data/snapshot/`.
3. Scoring:
   - `app/scoring/metrics.py`: daily data → annual metrics.
   - `app/scoring/engine.py`: composite score from observed hazards plus declarations, with fixed-cap normalization, tiers and a breakdown.
4. API: `GET /rankings?top_n=10` (no LLM). `POST /chat` with **one tool**, `rank_hubs`, and a minimal `AgentAnswer` structured output (answer_markdown, ranking, assumptions, confidence).
5. `app/static/index.html`: a text box and a ranking table.
6. Tests: pytest for metrics and scoring, plus 3 eval cases ("most dangerous hubs", "top 5", one out-of-scope question).

**Done when:** in the browser, *"What are the 10 most dangerous hubs?"* returns a ranking that matches `/rankings` exactly, with drivers and assumptions shown. After that, deploy to Render right away, so deployment risk surfaces early.

## Later slices (each extends the same base)
- **Slice 2:** hazard and region filters (Midwest winter) plus NRI
- **Slice 3:** `compare_hubs` + `explain_hub_score` (Miami vs Houston, why Dallas)
- **Slice 4:** `weather_stat` (Denver snow %) + conversation memory for follow-ups
- **Slice 5:** full eval set
- **Slice 6:** NWS operational score + alerts webhook
- **Slice 7:** voice, docs, session export

## Verification for slice 1
- `make test`: metrics on a fixture (e.g. 3 snow days out of 10 → 30%), cap and tier boundaries.
- `make ingest`: row counts are about 41 × 1,826 daily rows, with no hub missing.
- `curl /rankings` and `curl -X POST /chat`: the LLM ranking order equals the deterministic order.
- `make eval`: 3/3 pass. Then repeat the manual check on the Render URL.
