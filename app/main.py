"""FastAPI service: deterministic endpoints + the chat agent.

    uv run uvicorn app.main:app --reload       ->  http://127.0.0.1:8000/docs
"""
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from app import db
from app.agent.loop import AgentError, AgentResult, ask
from app.agent.schemas import Hazard, Region
from app.scoring import service
from app.scoring.disruption import HubDisruption, rank

HISTORY_TURNS = 6  # earlier question/answer pairs sent with each new question


@asynccontextmanager
async def lifespan(app: FastAPI):
    service.load_results(restore=True)  # rebuild SQLite from the committed snapshot, then score
    yield


app = FastAPI(title="Weather Risk Intelligence Agent", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------- deterministic (no LLM)

@app.get("/health")
def health():
    res = service.results()
    return {"status": "ok", "hubs": len(res), "hubs_complete": sum(r.weather_complete for r in res)}


@app.get("/rankings")
def rankings(hazard: Hazard = "total", region: Region | None = None, top_n: int = Query(10, ge=1, le=50)):
    """Hubs ranked by expected disruption days per year, computed by the rule engine only."""
    return rank(service.results(), hazard, region, top_n)


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
        raise HTTPException(502, str(exc)) from exc
    _save(session_id, req.message, result.answer.model_dump_json())
    return ChatResponse(session_id=session_id, result=result)
