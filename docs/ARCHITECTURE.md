# System architecture

Legend: **solid boxes** are built, **dashed boxes** are planned (later slices).
GitHub renders the diagrams below; PNG/SVG exports are in `docs/img/` (for slides).

## 1. Components and how they talk to each other

```mermaid
---
config:
  flowchart:
    subGraphTitleMargin: {top: 6, bottom: 30}
    wrappingWidth: 400
---
flowchart TB
    subgraph SRC["Layer 1 - Public APIs"]
        direction LR
        OM["Open-Meteo<br/>historical weather (daily)"]
        FEMA["OpenFEMA<br/>disaster declarations"]
        NWS["NWS<br/>live alerts + forecast"]:::planned
    end

    subgraph BATCH["Layer 2 - Ingestion (offline CLI)"]
        direction LR
        CFG["config/hubs.yaml<br/>50 hubs"] --> ING["app/ingest.py<br/>fetch + validate<br/>(Pydantic)"]
        ING <--> RAW[("data/raw/*.json<br/>raw HTTP cache")]
        ING -- export --> SNAP[("data/snapshot/<br/>*.parquet (git)")]
    end

    subgraph SVC["Layer 3 - FastAPI service (1 container)"]
        direction LR
        DB[("SQLite")] --> ENG["Scoring engine<br/>deterministic,<br/>no LLM"]
        ENG --> TOOLS["Agent tools<br/>rank / compare /<br/>explain / weather_stat"]:::planned
        TOOLS <-- JSON --> AGENT["Agent loop<br/>tool use +<br/>AgentAnswer schema"]:::planned
        AGENT --> API["REST API<br/>/chat /rankings<br/>/hubs /alerts"]:::planned
        ENG -- "/rankings (no LLM)" --> API
        SCHED["Scheduler (hourly)<br/>risk change detection"]:::planned --> DB
        RULES["config/<br/>disruption.yaml"] --> ENG
    end

    CLAUDE["Anthropic API<br/>Claude"]:::planned

    subgraph CLI["Layer 4 - Clients"]
        direction LR
        UI["Chat UI<br/>HTML/JS + voice"]:::planned
        EVAL["Eval runner"]:::planned
        HOOK["Webhook alert<br/>(Slack)"]:::planned
    end

    OM -- "1 call per hub" --> ING
    FEMA -- "1 call per county" --> ING
    SNAP -- "restore at startup" --> DB
    NWS --> SCHED
    AGENT <-- "Messages API" --> CLAUDE
    API <-- "HTTP JSON" --> UI
    API <-- "HTTP JSON" --> EVAL
    SCHED -- "POST on change" --> HOOK

    classDef planned stroke-dasharray: 5 5,stroke:#888,color:#555;
```

**Main design choice: batch ingestion is separate from online serving.** The web service never
calls a weather API to answer a question about history. It reads SQLite, which is rebuilt from
the committed snapshot at startup. As a result the demo works even if a public API is down or
rate-limited, answers are fast, and evals are repeatable.

## 2. One chat question, step by step (planned flow)

```mermaid
sequenceDiagram
    autonumber
    actor U as Analyst
    participant UI as Chat UI
    participant API as FastAPI /chat
    participant A as Agent loop
    participant C as Claude API
    participant T as Tools
    participant E as Scoring engine
    participant DB as SQLite

    U->>UI: "What are the 10 most dangerous hubs?"
    UI->>API: POST /chat {session_id, message}
    API->>DB: load conversation history
    API->>A: history + new message
    A->>C: messages + tool schemas (strict JSON)
    C-->>A: tool_use rank_hubs{hazard:"total", top_n:10}
    A->>T: validate input (Pydantic), run tool
    T->>E: rank(results, "total", None, 10)
    E->>DB: weather + declarations (read)
    E-->>T: ranking + per-hazard breakdown
    T-->>A: JSON result
    A->>C: tool_result
    C-->>A: final answer in AgentAnswer JSON schema
    A->>A: validate with Pydantic (retry once on failure)
    A->>DB: save turn
    A-->>API: AgentAnswer
    API-->>UI: JSON (answer, ranking, drivers, assumptions, confidence)
    UI-->>U: text + ranking table + sources/assumptions panel
```

The LLM never produces a number: it chooses tools and writes the explanation. All numbers come
from the scoring engine, and the eval checks that the LLM's ranking matches the engine's.

## 3. Component summary

| Component | Technology | Status | Why this choice |
|---|---|---|---|
| Hub list | YAML, validated by Pydantic | built | business config, editable without code |
| Ingestion | Python CLI, httpx + tenacity retries | built | runs where the API quota allows (laptop, cron); decoupled from serving |
| Raw cache | JSON files on disk | built | protects API quota, audit trail ("where did this number come from?") |
| Storage | SQLite + parquet snapshot in git | built | small relational data, no server to run, reproducible |
| Scoring | Pure Python + pandas, rules in YAML | built | deterministic and explainable; analysts edit thresholds, not code |
| Report CLI | `python -m app.scoring.report` | built | ranking without the LLM (sanity check, demo) |
| REST API | FastAPI | planned | Pydantic-native, auto `/docs` |
| Agent | Anthropic SDK, manual tool-use loop | planned | ~100 lines, transparent, no framework to explain |
| Structured output | Pydantic `AgentAnswer` -> JSON schema | planned | one schema for the LLM contract and the validation |
| Chat UI | One static HTML/JS page served by FastAPI | planned | one process, one URL; voice via browser Web Speech API |
| Alerts | APScheduler in-process + webhook POST | planned | no extra infrastructure (a cron would be needed with several replicas) |
| Evals | YAML cases + Python runner | planned | checks tool choice, hubs, ranking fidelity, numbers, scoping |
| Deployment | Docker on Render | planned | one container; DB rebuilt from the snapshot at startup |
