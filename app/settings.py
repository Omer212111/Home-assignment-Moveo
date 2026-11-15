"""Runtime settings, read from environment variables or a .env file and validated by Pydantic."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # LLM
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "low"  # chat latency matters more than deep reasoning here; tools do the maths

    # Storage
    db_path: Path = ROOT / "data" / "weather_risk.db"
    snapshot_dir: Path = ROOT / "data" / "snapshot"
    raw_cache_dir: Path = ROOT / "data" / "raw"

    # Config files
    hubs_file: Path = ROOT / "config" / "hubs.yaml"
    scoring_file: Path = ROOT / "config" / "disruption.yaml"

    # Data window: 5 full calendar years (see docs/PLAN.md, "History window")
    history_start: str = "2021-01-01"
    history_end: str = "2025-12-31"
    declarations_since: str = "2006-01-01"  # 20 years of FEMA declarations

    # Optional paid Open-Meteo key: switches to the commercial endpoint (free tier is non-commercial)
    open_meteo_api_key: str | None = None


settings = Settings()
