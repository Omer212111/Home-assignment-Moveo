"""FastAPI service: deterministic endpoints + the chat agent.

    uv run uvicorn app.main:app --reload       ->  http://127.0.0.1:8000/docs
"""
import asyncio
import base64
import secrets
import logging
import uuid
from typing import Literal
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import alerts, db
from app.agent import trajectory
from app.agent.loop import AgentError, AgentResult, ask
from app.agent.schemas import Hazard, Region, Season
from app.scoring import store
from app.settings import settings
from app.scoring.periods import SEASONS
from app.scoring import service
from app.scoring.disruption import HubDisruption, rank

log = logging.getLogger("weather_agent")
HISTORY_TURNS = 6  # earlier question/answer pairs sent with each new question


async def _alerts_loop():
    """Scheduled alerts, every tick: create the monthly outlook if due (catch-up after downtime) and
    send pending alerts (retries). Runs in a worker thread so the event loop never blocks. Safe with
    restarts and several replicas: creation is deduplicated and delivery claims alerts with a lease."""
    while True:
        try:
            await asyncio.to_thread(alerts.check_outlook)
            await asyncio.to_thread(alerts.deliver_pending)
        except Exception:
            log.exception("alerts tick failed")
        await asyncio.sleep(settings.alerts_check_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    service.load_results(restore=True)  # rebuild SQLite from the committed snapshot, then score
    task = asyncio.create_task(_alerts_loop())
    yield
    task.cancel()


app = FastAPI(title="Weather Risk Intelligence Agent", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def password_protection(request: Request, call_next):
    """Optional protection for a public URL: with APP_PASSWORD set, every page and endpoint asks for
    it (HTTP Basic auth: the browser shows its own login prompt; any user name). /chat spends API
    credits, so a public demo should not be open to anyone. /health stays open for health checks."""
    if not settings.app_password or request.url.path == "/health":
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            _, _, password = base64.b64decode(auth[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            password = ""
        if secrets.compare_digest(password.encode(), settings.app_password.encode()):
            return await call_next(request)
    return Response("Authentication required", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Hub Weather Risk"'})


STATIC = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def index():
    """The chat UI: one static page that calls /chat."""
    return FileResponse(STATIC / "index.html")


# ---------------------------------------------------------------- deterministic (no LLM)

@app.get("/health")
def health():
    res = service.results()
    run = store.active_run()
    return {"status": "ok", "hubs": len(res), "hubs_complete": sum(r.weather_complete for r in res),
            "scoring_run": service.run_id(), "data_as_of": run and run["data_as_of"]}


@app.get("/rankings")
def rankings(hazard: Hazard = "total", region: Region | None = None, top_n: int = Query(10, ge=1, le=50),
             season: Season | None = None, order: Literal["most", "least"] = "most"):
    """Hubs ranked by expected disruption days per year, computed by the rule engine only."""
    return rank(service.results(), hazard, region, top_n, SEASONS[season] if season else None, order)


@app.get("/health/llm")
def health_llm():
    """Checks the API key and the connection to Anthropic without spending tokens (model lookup).
    Behind APP_PASSWORD when it is set (unlike /health)."""
    from app.agent.loop import check_llm
    return check_llm()


@app.get("/alerts")
def list_alerts(limit: int = Query(20, ge=1, le=100)):
    """Recent alerts (monthly outlooks and score changes), newest first."""
    return {"alerts": alerts.recent(limit), "webhook_configured": bool(settings.alert_webhook_url)}


@app.post("/alerts/check")
def check_alerts(x_alerts_token: str | None = Header(default=None)):
    """Webhook-triggered check (e.g. from cron or a CI job): run the monthly outlook now.
    Idempotent: returns the existing alert if this month's outlook was already sent."""
    if settings.alerts_token and x_alerts_token != settings.alerts_token:
        raise HTTPException(401, "invalid X-Alerts-Token")
    alert_id = alerts.check_outlook()
    return {"created_alert_id": alert_id, "already_sent": alert_id is None,
            "outlook_for": "%d-%02d" % alerts.outlook_month()}


@app.get("/hubs/{hub_id}", response_model=HubDisruption)
def hub(hub_id: str):
    r = service.by_id(hub_id)
    if r is None:
        raise HTTPException(404, f"unknown hub '{hub_id}'")
    return r


# ---------------------------------------------------------------- chat

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    result: AgentResult


def _history(session_id: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT role, content FROM conversations WHERE session_id=? ORDER BY turn DESC LIMIT ?",
                            (session_id, HISTORY_TURNS * 2)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _save(session_id: str, question: str, answer_json: str):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM conversations WHERE session_id=?", (session_id,)).fetchone()[0]
        conn.executemany("INSERT INTO conversations VALUES (?,?,?,?,?)",
                         [(session_id, n, "user", question, now), (session_id, n + 1, "assistant", answer_json, now)])


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    session_id = req.session_id or uuid.uuid4().hex
    try:
        result = ask(req.message, _history(session_id))
    except AgentError as exc:
        log.warning("agent error: %s", exc)
        try:
            log.warning("trajectory of the failure: %s", trajectory.save(req.message, session_id, None, error=exc))
        except OSError:
            pass
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # unexpected: full traceback in the server log, short message to the client
        log.exception("unexpected error in /chat")
        raise HTTPException(500, f"Unexpected error: {type(exc).__name__}: {exc}") from exc
    _save(session_id, req.message, result.answer.model_dump_json())
    try:  # a trajectory file is a debugging aid: never fail the answer because of it
        trajectory.save(req.message, session_id, result)
    except OSError as exc:
        log.warning("could not save trajectory: %s", exc)
    return ChatResponse(session_id=session_id, result=result)
