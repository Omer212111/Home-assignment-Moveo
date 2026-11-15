# One container: FastAPI + agent + chat page. The data is in the image (data/snapshot/), so the
# container never calls a weather API: SQLite is rebuilt from the snapshot and scored at startup.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir uv==0.11.32

WORKDIR /app

# Dependencies first (cached layer), from the lock file, without dev tools (pytest, fakeredis).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY config ./config
COPY data/snapshot ./data/snapshot

# Run as a non-root user; it owns data/ (SQLite file, trajectories).
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

ENV PATH="/app/.venv/bin:$PATH" PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4)"

# One worker: the score cache and the alert loop live in the process (see docs/DESIGN_DECISIONS.md).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
