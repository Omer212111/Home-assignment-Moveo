# Data model: tables and how they connect

All data the system stores, across its storage layers (see README section 9):
the raw API cache (Redis or `data/raw/` files), the SQLite tables (restored from the committed
parquet snapshot at startup), and the objects the scoring engine keeps in memory.
PNG/SVG exports: `docs/img/data-model.png`, `docs/img/data-model.svg`.

```mermaid
erDiagram
    HTTPCACHE {
        string key PK "httpcache:source:hash (Redis) or data/raw/source/hash.json"
        string url "request URL, API key excluded"
        json params
        string fetched_at
        json data "the raw API response body"
    }
    HUBS {
        string hub_id PK "e.g. denver"
        string city
        string state
        float lat
        float lon
        string county_fips "joins to declarations"
        string region "US Census region"
        int pop_rank
    }
    DAILY_WEATHER {
        string hub_id PK, FK
        string date PK "2021-01-01 .. 2025-12-31"
        float tmax "C"
        float tmin "C"
        float precip_sum "mm"
        float rain_sum "mm"
        float snowfall_sum "cm"
        float wind_gusts_max "km/h"
        float wind_speed_max "km/h"
        int weather_code
    }
    DISASTER_DECLARATIONS {
        int disaster_number PK
        string county_fips PK
        string incident_type "Hurricane, Severe Storm, ..."
        string declaration_type "DR, EM, FM"
        string title
        string declaration_date
        string incident_begin_date
    }
    INGEST_RUNS {
        int run_id PK
        string source "weather or declarations"
        string params "hub or county, dates"
        string started_at
        string finished_at
        int rows
        string status
        string detail "cache or api + cache location"
    }
    CONVERSATIONS {
        string session_id PK
        int turn PK
        string role "user or assistant"
        string content "question, or the answer JSON"
        string created_at
    }
    HUB_DISRUPTION {
        string hub_id PK, FK "in memory, computed at startup"
        float total_days_per_year "the KPI"
        json hazards "per hazard: days, drivers, events"
        json declarations_counted "FEMA disasters used"
        float weather_coverage
    }
    DAY_RECORD {
        string hub_id FK "in memory"
        string date "disruption day, or disaster start"
        string hazard
        float value "severity, or days per disaster"
        string channel "weather or declarations"
        string label "the rule behind it"
        string event "event or disaster id"
    }

    HTTPCACHE ||--o{ DAILY_WEATHER : "1 response per hub, parsed into 1,826 days"
    HTTPCACHE ||--o{ DISASTER_DECLARATIONS : "1 response per county"
    INGEST_RUNS }o--|| HTTPCACHE : "logs each fetch and where it came from"
    HUBS ||--o{ DAILY_WEATHER : "hub_id"
    HUBS }o--o{ DISASTER_DECLARATIONS : "county_fips (join, not a foreign key)"
    HUBS ||--|| HUB_DISRUPTION : "scored at startup"
    HUB_DISRUPTION ||--o{ DAY_RECORD : "keeps its records"
    DAILY_WEATHER ||--o{ DAY_RECORD : "weather channel: rule triggers + recovery days"
    DISASTER_DECLARATIONS ||--o{ DAY_RECORD : "declarations channel: counted DR disasters"
```

## Tables

| Table | Storage | Rows | Written by | Read by |
|---|---|---|---|---|
| `httpcache` | Redis (`REDIS_URL`) or `data/raw/*.json` (git) | 97 (50 + 47) | ingestion | ingestion only |
| `hubs` | SQLite, from `config/hubs.yaml`; parquet snapshot | 50 | ingestion | engine, API |
| `daily_weather` | SQLite; parquet snapshot | 91,300 | ingestion (Open-Meteo) | engine, `weather_stat` |
| `disaster_declarations` | SQLite; parquet snapshot | 669 rows: 481 distinct disasters, one row per disaster and county | ingestion (OpenFEMA) | engine |
| `ingest_runs` | SQLite; parquet snapshot | one per fetch | ingestion | `--status`, audit |
| `conversations` | SQLite only (not committed) | 2 per chat question | `/chat` | `/chat` (last 6 turns) |
| `HubDisruption` | memory | 50 | engine at startup | all tools, `/rankings`, `/hubs` |
| `DayRecord` | memory (inside `HubDisruption`) | one per disruption day or counted disaster | engine | month / season / year breakdowns |

## Connections worth knowing
- **Hubs to declarations is many-to-many through the county.** 50 hubs sit in 47 counties
  (Phoenix and Mesa share Maricopa County, Los Angeles and Long Beach share Los Angeles County,
  Fort Worth and Arlington share Tarrant County),
  and a county has many disasters. It is a join on `county_fips`, not a foreign key.
- **One cache entry per API request:** Open-Meteo is fetched per hub (one response = 1,826 daily
  rows), OpenFEMA per county. The cache key hashes the URL and parameters (API key excluded).
- **`DayRecord` is the bridge to time breakdowns:** every disruption day (weather) and every counted
  disaster (declarations) is one record, so the annual KPI and every month, season or year
  breakdown are sums over the same records.
- **Not shown:** `hub_scores` exists in the schema but is no longer written (scores moved to memory);
  it is the natural place to store score history for the planned risk-change alerts.
  Chat trajectories are JSON files (`data/trajectories/`), not a table.
