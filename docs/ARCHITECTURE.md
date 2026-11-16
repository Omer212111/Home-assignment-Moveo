# System architecture

Everything below is built and running (live: https://hub-weather-risk.onrender.com/, password protected).
GitHub renders the diagrams; PNG/SVG exports are in `docs/img/` (`architecture-1`, `architecture-2`,
`architecture-3`). A one-page summary is in `docs/DESIGN_SUMMARY.pdf`; the reasons behind every choice
are in `docs/DESIGN_DECISIONS.md`.

## 1. Components and how they talk to each other

```mermaid
---
config:
  flowchart:
    curve: basis
    nodeSpacing: 28
    rankSpacing: 45
---
flowchart LR

    subgraph SVC["FastAPI service · one container, one process"]
        direction TB
        DB[("<b>SQLite</b> (WAL)<br/>source tables · scoring runs<br/>conversations · alerts")]
        ENG["<b>Scoring service</b><br/>active scoring run, cached in memory"]
        CHAT["<b>POST /chat</b>"]
        AGENT["<b>Agent loop</b><br/>strict tool schemas · ModelAnswer<br/>guardrails · trajectories"]
        TOOLS["<b>5 tools</b><br/>rank · explain · compare · breakdown · weather_stat"]
        REST["<b>GET /rankings · /hubs · /health</b><br/>no LLM"]
        ALERT["<b>Alerts</b><br/>monthly outlook · score change<br/>outbox delivery"]
        DB --> ENG
        CHAT --> AGENT --> TOOLS --> ENG
        REST --> ENG
        ENG --> ALERT
        ALERT <--> DB
        CHAT <-. "history" .-> DB
    end

    UI["<b>Chat page</b><br/>static HTML/JS<br/>answer · details · agent steps · alerts"]
    CLAUDE["<b>Anthropic API</b><br/>Claude Sonnet 5.5"]
    HOOK["<b>Webhook</b><br/>Slack-compatible JSON"]
    EVAL["<b>Eval runner</b><br/>24 cases · history"]

    UI -- "HTTP JSON" --> CHAT
    EVAL -- "same agent loop" --> AGENT
    AGENT <-- "Messages API" --> CLAUDE
    ALERT -- "POST" --> HOOK

    classDef client fill:#f3eefa,stroke:#7d5bb5,color:#271a3b
    classDef api fill:#e8f1fb,stroke:#3b78c3,color:#13263d
    classDef agent fill:#fff4e5,stroke:#d9822b,color:#3d2a12
    classDef ext fill:#fdecec,stroke:#c0504d,color:#3b1414
    classDef eng fill:#eaf6ec,stroke:#3c9a52,color:#14301b
    classDef batch fill:#eef3f3,stroke:#5b8a8a,color:#1d2e2e
    classDef group fill:#fafafa,stroke:#c8c8c8,color:#444
    class UI,EVAL client
    class CHAT,REST api
    class AGENT,TOOLS,ALERT agent
    class CLAUDE,HOOK ext
    class ENG,DB eng
    class SVC group
```

**Main design choice: no weather API at runtime.** The service never calls a weather API to answer
a question. It starts from the committed parquet snapshot, so the demo works when a public API is
down or rate-limited, answers are fast, and evals run on identical data every time. How the data is
downloaded, refreshed and deployed is a setup step (README section 1, "Refreshing the data").
`GET /rankings` and `/hubs` are the deterministic API for other consumers (BI, scripts, Swagger at
`/docs`); the chat page uses `POST /chat` and `GET /alerts`.

## 2. One chat question, step by step

```mermaid
sequenceDiagram
    autonumber
    actor U as Analyst
    participant UI as Chat page
    participant API as FastAPI /chat
    participant A as Agent loop
    participant C as Claude Sonnet 5.5
    participant T as Tools
    participant S as Scoring service
    participant DB as SQLite

    U->>UI: "Why is the Dallas hub's risk high?"
    UI->>API: POST /chat {session_id, message}
    API->>DB: last 6 question/answer pairs
    API->>A: history + question
    A->>C: system prompt, 5 strict tool schemas, ModelAnswer schema (prompt cache)
    C-->>A: tool_use explain_hub{hub_id: "dallas"}
    A->>T: validate arguments (Pydantic), invalid → error back to the model
    T->>S: active scoring run (in memory)
    S-->>T: days, index, tier, rank 17 of 50, drivers, FEMA disasters
    T-->>A: JSON result (pre-computed numbers, caveats, method, scoring run)
    A->>C: tool_result
    Note over A,C: at most 2 tool rounds, a repeat-only round or the 6th call<br/>forces the answer with tool_choice none
    C-->>A: final answer as ModelAnswer JSON
    A->>A: validate (1 retry), attach the engine's ranking if rank_hubs ran
    A->>DB: save the turn
    A->>A: save the trajectory (JSON file)
    A-->>API: AgentAnswer + tool calls + tokens + trajectory
    API-->>UI: JSON
    UI-->>U: answer · details panel built from the engine data · agent steps
```

The LLM never produces a number: it chooses tools and writes the explanation; every number comes from
the engine, and the evals check it (`docs/EVALS.md`).

## 3. Background flows: startup, scoring runs, alerts

```mermaid
flowchart TB
    START["<b>Server start</b>"] --> RESTORE["restore the snapshot into SQLite"]
    RESTORE --> MATCH{"active scoring run made from<br/>the current rules and data?"}
    MATCH -- yes --> LOAD["load it (0.05 s)"]
    MATCH -- no --> SCORE["score all hubs (~1 s) → store a run<br/>activate it in one transaction (lock + re-check)"]
    SCORE --> CHANGE["score-change alert stored<br/>in the same transaction"]
    LOAD --> SERVE["serve requests from the cached run<br/>(reload within 5 s if another run becomes active)"]
    CHANGE --> SERVE


    TICK["<b>Alert loop</b> every 60 s<br/>(+ POST /alerts/check)"] --> OUTLOOK["monthly outlook due?<br/>(7 days before the month, dedup key per month)"]
    TICK --> SEND["deliver pending alerts<br/>(claim with a lease, retry, at-least-once)"]
    OUTLOOK --> SEND
    CHANGE --> SEND
    SEND --> WEBHOOK["webhook POST · GET /alerts · page"]

    classDef step fill:#eaf6ec,stroke:#3c9a52,color:#14301b
    classDef alert fill:#fff4e5,stroke:#d9822b,color:#3d2a12
    classDef start fill:#e8f1fb,stroke:#3b78c3,color:#13263d
    class START,TICK start
    class RESTORE,MATCH,LOAD,SCORE,SERVE step
    class CHANGE,OUTLOOK,SEND,WEBHOOK alert
```

## 4. Component summary

| Component | Technology | Why this choice |
|---|---|---|
| Hub list | `config/hubs.yaml`, validated by Pydantic | business config, editable without code |
| Storage | parquet snapshot in git (restored at startup) + SQLite (WAL) | small relational data, no server to run, reproducible; plain SQL for a later Postgres move |
| Scoring engine | pure Python + pandas, rules in `config/disruption.yaml` | deterministic and explainable; analysts edit thresholds, not code |
| Scoring runs | SQLite tables, one set per run (rules hash + data fingerprint) | every score traceable and comparable; computed on write, cached in memory |
| Index and tiers | fixed reference in the rules (100 = median hub) | comparable, stable scores; pro-rated per month and season |
| REST API | FastAPI | Pydantic-native, `/docs`, one process for page, API and agent |
| Agent | Anthropic SDK, hand-written tool-use loop | transparent, every step logged (trajectories); guardrails from the eval history |
| JSON contract | Pydantic tool inputs (strict) + `ModelAnswer` structured output | one definition for the schema sent to Claude and the validation |
| Chat page | one static HTML/JS page (desktop) | no build step, a plain client of `POST /chat`; details built from engine data |
| Alerts | in-process loop + `POST /alerts/check`, outbox table, webhook | no extra infrastructure; exactly-once creation, at-least-once delivery |
| Evals | YAML cases + Python runner, history and comparison | correctness vs efficiency, pass rates over repeats |
| CI | GitHub Actions | the offline tests on every push |
| Deployment | Docker on Render (Blueprint), optional site password | one container; the snapshot is in the image; auto-deploy on push |
