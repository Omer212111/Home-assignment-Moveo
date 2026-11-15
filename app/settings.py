"""Runtime settings, read from environment variables or a .env file and validated by Pydantic."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # LLM
    llm_model: str = "claude-sonnet-5-5"  # cheaper than Opus 5.5; reliable tool use + JSON schema
    llm_effort: str = "low"  # chat latency matters more than deep reasoning here; tools do the maths
    # "summarized" returns a readable summary of the model's thinking (for trajectories and the UI);
    # "omitted" (the API default) returns empty thinking blocks. Billing is the same either way.
    llm_thinking_display: str = "summarized"
    # API key: read from .env or the environment. AGENT_ANTHROPIC_API_KEY wins, so a hosted
    # environment can set the agent's key without touching a tool's own ANTHROPIC_API_KEY.
    agent_anthropic_api_key: str | None = None
    anthropic_api_key: str | None = None

    # Storage
    db_path: Path = ROOT / "data" / "weather_risk.db"
    snapshot_dir: Path = ROOT / "data" / "snapshot"
    raw_cache_dir: Path = ROOT / "data" / "raw"
    # Raw API response cache in Redis instead of data/raw/ files, e.g. redis://localhost:6379/0
    redis_url: str | None = None
    trajectory_dir: Path = ROOT / "data" / "trajectories"  # one JSON per chat question (git-ignored)

    # Config files
    hubs_file: Path = ROOT / "config" / "hubs.yaml"
    scoring_file: Path = ROOT / "config" / "disruption.yaml"
    alerts_file: Path = ROOT / "config" / "alerts.yaml"

    # Alerts: outgoing webhook (Slack-compatible JSON; any receiver works). Unset: alerts are only
    # stored and shown at GET /alerts. ALERTS_TOKEN protects POST /alerts/check when set.
    alert_webhook_url: str | None = None
    alerts_token: str | None = None
    alerts_check_interval_seconds: int = 60   # outlook check + retry of pending deliveries

    # Data window: 5 full calendar years (see docs/PLAN.md, "History window")
    history_start: str = "2021-01-01"
    history_end: str = "2025-12-31"
    declarations_since: str = "2006-01-01"  # 20 years of FEMA declarations

    # Optional paid Open-Meteo key: switches to the commercial endpoint (free tier is non-commercial)
    open_meteo_api_key: str | None = None


settings = Settings()
