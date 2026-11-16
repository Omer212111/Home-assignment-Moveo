# Weather Risk Intelligence Agent: Overview

**Live demo:** https://hub-weather-risk.onrender.com/ (any user name, password `Omer2106!`) ·
**Code:** https://github.com/Omer212111/Home-assignment-Moveo

## The problem

A logistics company runs distribution hubs across the US and each year picks a few for
resilience upgrades. This project helps analysts decide which hubs are most exposed to weather
disruption. They can ask questions in plain English and follow up, for example "Which Midwest hubs
are most exposed to winter disruption?" or "Why is Dallas's risk high?".

## The approach

**A deterministic KPI: expected disruption days per year.** Each hub gets a number of operating days
lost per year. The number is in business units, it adds up across hazards, and it can later be
multiplied by a cost per lost day. A rules engine computes it from two public sources:

- **Open-Meteo:** 5 years of daily weather. This covers frequent events such as snow, ice, cold,
  heavy rain, wind and heat.
- **OpenFEMA:** 20 years of disaster declarations. This covers rare events such as hurricanes and
  severe storms.

The thresholds live in a YAML file, so analysts can change numbers without touching code. Each score
also gets an index (100 = the median hub) and a tier. The scores can be broken down by month,
season and year.

**An AI agent on top.** Claude chooses among five tools: rank, explain, compare, breakdown and
weather statistics. It then explains the result in plain language. **The LLM never produces a
number:** every figure comes from the engine, and the evals check that each number in an answer
comes from a tool result.

## What was built

- **Agent:** a hand-written tool-use loop, without an agent framework. The loop is simple, and a
  framework would have added dependency problems and setup time. Tool inputs and the final answer follow strict JSON
  schemas, validated with Pydantic. The final answer includes the assumptions made, a confidence
  level and notes on uncertainty. Guardrails limit the loop to at most 2 rounds of tool calls.
- **API and chat page:** FastAPI serves `POST /chat` for the agent and `/rankings` and `/hubs`
  without the LLM. The chat page shows the answer, the engine's data and each step the agent took.
- **Alerts (bonus):** a monthly risk outlook and alerts when a hub's score changes, posted to a
  webhook.
- **Evaluation:** a set of 24 cases. It covers the four assignment questions, follow-up questions,
  questions out of scope, and robustness cases such as misspelled names and prompt injection. Seven
  runs are documented, and each one led to a fix. There are also 92 offline tests that run in CI.
- **Deployment:** one Docker container on Render, protected by a password.

## Key design choices

- **A data snapshot instead of live API calls.** The free weather API limits the number of
  requests, and the quota ran out during the project. So the data is downloaded once and committed
  to the repository; the service starts from that snapshot. Answers are fast and reproducible, and
  the demo keeps working when a public API is down or out of quota.
- **SQLite instead of a database server.** This choice was made partly to finish within the 24-hour
  deadline and to keep the deployment simple and safe.
- **Narrow scope, done well.** The work concentrates on clear, explainable scores rather than broad
  features.

## Assumptions and limitations

- **The hubs are my choice.** The assignment does not list the company's hubs. I decided to use the
  50 most populous US cities (Census 2023 estimates), with one hub at each city centre.
- The thresholds are expert judgment. They have not been fitted to the company's own downtime data.
- Five years of weather is a small sample.
- Hurricanes are measured by how often disasters were declared, not by the size of their worst
  storms.
- The model measures **exposure**, not vulnerability or cost. It does not know a hub's building
  standard or backup power.

## How it was built

The project was built with Claude Code over three sessions. The links to those sessions are in
README section 13. The design documents are in `docs/`.
