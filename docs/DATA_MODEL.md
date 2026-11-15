# Data model: tables and how they connect

The data flows left to right: inputs (the hub list and the raw API cache), the SQLite tables
(restored from the committed parquet snapshot at startup), and the objects the scoring engine
keeps in memory. PNG/SVG exports: `docs/img/data-model.png`, `docs/img/data-model.svg`.

```mermaid
---
config:
  flowchart:
    curve: basis
    nodeSpacing: 25
    rankSpacing: 60
---
flowchart LR
    subgraph L1["1 · Inputs"]
        direction TB
        Y["<b>config/hubs.yaml</b><br/>50 hubs<br/><i>edited by analysts</i>"]
        HC["<b>HTTPCACHE</b><br/>Redis or data/raw/ files<br/>97 API responses<br/><i>original data, audit trail</i>"]
    end

    subgraph L2["2 · SQLite (from the parquet snapshot)"]
        direction TB
        H["<b>HUBS</b><br/>key: hub_id · 50 rows<br/><i>where each hub is</i>"]
        W["<b>DAILY_WEATHER</b><br/>key: hub_id + date · 91,300 rows<br/><i>what the weather was</i>"]
        D["<b>DISASTER_DECLARATIONS</b><br/>key: disaster + county · 669 rows<br/><i>which disasters hit the county</i>"]
        I["<b>INGEST_RUNS</b><br/>one row per download<br/><i>when the data was loaded</i>"]
        C["<b>CONVERSATIONS</b><br/>key: session + turn<br/><i>chat history for follow-ups</i>"]
    end

    subgraph L3["3 · Memory (computed at startup)"]
        direction TB
        R["<b>DAY_RECORD</b><br/>one per disruption day or disaster<br/><i>when, which hazard, why</i>"]
        K["<b>HUB_DISRUPTION</b><br/>one per hub<br/><i>the KPI: days per year</i>"]
    end

    Y --> H
    HC -- "per hub" --> W
    HC -- "per county" --> D
    HC -. "logged" .-> I
    H -- "hub_id" --> W
    H -- "county_fips" --> D
    W -- "weather rules" --> R
    D -- "counted disasters" --> R
    R -- "sum per hub" --> K

    classDef cache fill:#fff4e5,stroke:#d9822b,color:#3d2a12
    classDef sql fill:#e8f1fb,stroke:#3b78c3,color:#13263d
    classDef mem fill:#eaf6ec,stroke:#3c9a52,color:#14301b
    classDef chat fill:#f3eefa,stroke:#7d5bb5,color:#271a3b
    classDef layer fill:#fafafa,stroke:#c8c8c8,color:#444
    class HC,Y cache
    class H,W,D,I sql
    class C chat
    class R,K mem
    class L1,L2,L3 layer
```

## Purpose of each table

| Table | Layer | Purpose | Written by | Read by |
|---|---|---|---|---|
| `HTTPCACHE` | Redis (`REDIS_URL`) or `data/raw/*.json` (git) | Original API responses, never expire: audit trail and quota protection | ingestion | ingestion only |
| `HUBS` | SQLite + parquet snapshot | The 50 hubs: location, county, Census region; the table everything joins to | ingestion (from `config/hubs.yaml`) | engine, API |
| `DAILY_WEATHER` | SQLite + parquet snapshot | One row per hub per day, 2021-2025 | ingestion (Open-Meteo) | engine, `weather_stat` |
| `DISASTER_DECLARATIONS` | SQLite + parquet snapshot | FEMA disasters per county since 2006: the rare events 5 years of weather cannot show | ingestion (OpenFEMA) | engine |
| `INGEST_RUNS` | SQLite + parquet snapshot | Log of every download (source, rows, cache or API, status) | ingestion | `ingest --status`, audit |
| `CONVERSATIONS` | SQLite only (not committed) | Chat history, so follow-up questions have context | `/chat` | `/chat` (last 6 turns) |
| `DAY_RECORD` | memory | Every disruption day and counted disaster after the rules: the source of all time breakdowns | engine at startup | month / season / year tools |
| `HUB_DISRUPTION` | memory | The KPI per hub, split by hazard, with drivers | engine at startup | all tools, `/rankings`, `/hubs` |

## Columns

| Table | Columns (key in bold) |
|---|---|
| `HTTPCACHE` | **key** (`httpcache:<source>:<hash>`), url, params (API key excluded), fetched_at, data (raw body) |
| `HUBS` | **hub_id**, city, state, lat, lon, county_fips, region, pop_rank |
| `DAILY_WEATHER` | **hub_id**, **date**, tmax, tmin (C), precip_sum, rain_sum (mm), snowfall_sum (cm), wind_gusts_max, wind_speed_max (km/h), weather_code |
| `DISASTER_DECLARATIONS` | **disaster_number**, **county_fips**, incident_type, declaration_type (DR / EM / FM), title, declaration_date, incident_begin_date |
| `INGEST_RUNS` | **run_id**, source, params, started_at, finished_at, rows, status, detail (cache or API + cache location) |
| `CONVERSATIONS` | **session_id**, **turn**, role, content, created_at |
| `DAY_RECORD` | hub, date, hazard, value (severity, or days per disaster), channel (weather / declarations), label (rule), event |
| `HUB_DISRUPTION` | **hub_id**, total_days_per_year, hazards (days, drivers, events per hazard), declarations_counted, weather_coverage |

## Connections worth knowing
- **Hubs to declarations is many-to-many through the county.** 50 hubs sit in 47 counties
  (Phoenix and Mesa share Maricopa County, Los Angeles and Long Beach share Los Angeles County,
  Fort Worth and Arlington share Tarrant County), and a county has many disasters. It is a join on
  `county_fips`, not a foreign key.
- **One cache entry per API request:** Open-Meteo is fetched per hub (one response = 1,826 daily
  rows), OpenFEMA per county.
- **`DAY_RECORD` is the bridge to time breakdowns:** the annual KPI and every month, season or year
  breakdown are sums over the same records, so they always agree.
- **Not shown:** `hub_scores` exists in the schema but is no longer written (scores moved to memory);
  it is the natural place to store score history for the planned risk-change alerts.
  Chat trajectories are JSON files (`data/trajectories/`), not a table.


For the design at scale (thousands of hubs, decades of data, versioned rules), see `docs/DATA_MODEL_SCALABLE.md`.
