# Weather Risk Intelligence Agent

Ranks a logistics company's 50 US distribution hubs by expected weather disruption days per year,
with a Claude agent that answers questions about the ranking. (Full README coming; see `docs/`.)

## Quick start

```
uv sync
uv run uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/ for the chat, http://127.0.0.1:8000/docs for the API.
The chat needs `ANTHROPIC_API_KEY` in a `.env` file in the repository root.

- Tests (offline): `uv run pytest -q`
- Agent evals (real API calls): `uv run python -m evals.run` (one trajectory per turn in `evals/trajectories/`)
- How the agent thought on the last chat question: `uv run python -m app.agent.trajectory` (or tick "Agent steps" in the chat)

## Raw API cache in Redis (optional)

Ingestion caches every public-API response. By default the cache is `data/raw/` (files in git);
with `REDIS_URL` set it is Redis. Start Redis with persistence (Docker):

```
docker run -d --name redis -p 6379:6379 -v redis-data:/data redis:7 redis-server --appendonly yes
```

Then add `REDIS_URL=redis://localhost:6379/0` to `.env` and load the committed responses once:

```
uv run python -m app.sources.http_cache import-files
uv run python -m app.sources.http_cache status
```
