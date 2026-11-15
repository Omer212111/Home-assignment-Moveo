"""SQLite storage.

Why SQLite: the data is small (50 hubs x ~1.8k days), relational (joins by hub and
county), needs no server and ships with Python. The schema stays plain SQL so a
later move to Postgres is mechanical.
"""
import sqlite3
from contextlib import contextmanager

import pandas as pd

from app.settings import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS hubs (
    hub_id TEXT PRIMARY KEY, city TEXT, state TEXT, lat REAL, lon REAL,
    county_fips TEXT, region TEXT, pop_rank INTEGER
);
CREATE TABLE IF NOT EXISTS daily_weather (
    hub_id TEXT, date TEXT,
    tmax REAL, tmin REAL, precip_sum REAL, rain_sum REAL, snowfall_sum REAL,
    wind_gusts_max REAL, wind_speed_max REAL, weather_code INTEGER,
    PRIMARY KEY (hub_id, date)
);
CREATE TABLE IF NOT EXISTS disaster_declarations (
    disaster_number INTEGER, county_fips TEXT, incident_type TEXT, declaration_type TEXT,
    title TEXT, declaration_date TEXT, incident_begin_date TEXT,
    PRIMARY KEY (disaster_number, county_fips)
);
DROP TABLE IF EXISTS hub_scores;  -- an early design, never written; replaced by the scoring tables below

-- Scoring results (derived; not in the snapshot). Every score belongs to a scoring run, and a run
-- records exactly which rules and which data produced it. Exactly one run is 'active' at a time.
CREATE TABLE IF NOT EXISTS rule_sets (
    rule_set_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT UNIQUE NOT NULL,          -- hash of config/disruption.yaml
    rules_yaml TEXT NOT NULL,             -- the rules exactly as used
    first_used_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scoring_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_set_id INTEGER NOT NULL REFERENCES rule_sets(rule_set_id),
    data_fingerprint TEXT NOT NULL,       -- hash of the snapshot files the scores were computed from
    data_as_of TEXT,                      -- last weather date in the data
    status TEXT NOT NULL,                 -- running | active | superseded | failed
    hubs INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS hub_results (
    run_id INTEGER NOT NULL REFERENCES scoring_runs(run_id),
    hub_id TEXT NOT NULL,
    total_days_per_year REAL NOT NULL,    -- the KPI
    index_value REAL NOT NULL,            -- 100 = a typical hub (config index.reference_days)
    tier TEXT NOT NULL,
    weather_complete INTEGER NOT NULL,
    weather_coverage REAL,
    weather_period TEXT,
    weather_years REAL,
    declaration_years REAL,
    weather_days_deduplicated INTEGER,
    declarations_json TEXT,               -- FEMA disasters counted for this hub
    PRIMARY KEY (run_id, hub_id)
);
CREATE TABLE IF NOT EXISTS hazard_scores (
    run_id INTEGER NOT NULL,
    hub_id TEXT NOT NULL,
    hazard TEXT NOT NULL,
    label TEXT,
    days_per_year REAL NOT NULL,
    index_value REAL NOT NULL,
    tier TEXT NOT NULL,
    from_weather REAL,
    from_declarations REAL,
    events_per_year REAL,
    drivers_json TEXT,                    -- the rules behind the number, largest first
    PRIMARY KEY (run_id, hub_id, hazard),
    FOREIGN KEY (run_id, hub_id) REFERENCES hub_results(run_id, hub_id)
);
CREATE TABLE IF NOT EXISTS disruption_days (
    run_id INTEGER NOT NULL,
    hub_id TEXT NOT NULL,
    date TEXT NOT NULL,                   -- a disruption day, or the start of a counted disaster
    hazard TEXT NOT NULL,
    value REAL NOT NULL,                  -- severity of the day, or days counted for the disaster
    channel TEXT NOT NULL,                -- weather | declarations
    label TEXT,                           -- the rule behind it
    event TEXT NOT NULL,                  -- event or disaster id
    PRIMARY KEY (run_id, hub_id, date, hazard, event),
    FOREIGN KEY (run_id, hub_id) REFERENCES hub_results(run_id, hub_id)
);
CREATE TABLE IF NOT EXISTS monthly_scores (
    run_id INTEGER NOT NULL,
    hub_id TEXT NOT NULL,
    hazard TEXT NOT NULL,                 -- a hazard, or 'total'
    month INTEGER NOT NULL,               -- 1..12
    days_per_year REAL NOT NULL,          -- expected days per year falling in that month
    index_value REAL NOT NULL,            -- 100 = a typical hub's average month (reference / 12)
    tier TEXT NOT NULL,
    PRIMARY KEY (run_id, hub_id, hazard, month),
    FOREIGN KEY (run_id, hub_id) REFERENCES hub_results(run_id, hub_id)
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON scoring_runs(status);
CREATE TABLE IF NOT EXISTS conversations (
    session_id TEXT, turn INTEGER, role TEXT, content TEXT, created_at TEXT,
    PRIMARY KEY (session_id, turn)
);
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, params TEXT,
    started_at TEXT, finished_at TEXT, rows INTEGER, status TEXT, detail TEXT
);
"""

# Tables exported to / restored from the committed snapshot.
SNAPSHOT_TABLES = ["hubs", "daily_weather", "disaster_declarations", "ingest_runs"]


# Bump when the derived scoring tables change shape. They hold no source data (they are rebuilt from
# the snapshot and the rules), so an older version is simply dropped and recreated.
SCORING_SCHEMA_VERSION = 3
SCORING_TABLES = ["monthly_scores", "disruption_days", "hazard_scores", "hub_results", "scoring_runs", "rule_sets"]


def _migrate(conn) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCORING_SCHEMA_VERSION:
        for t in SCORING_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute(f"PRAGMA user_version = {SCORING_SCHEMA_VERSION}")


@contextmanager
def connect():
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        _migrate(conn)
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def read_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def export_snapshot() -> dict[str, int]:
    """Write the source tables to parquet so the app can start without calling any API."""
    settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for table in SNAPSHOT_TABLES:
        df = read_df(f"SELECT * FROM {table}")
        df.to_parquet(settings.snapshot_dir / f"{table}.parquet", index=False)
        counts[table] = len(df)
    return counts


def restore_snapshot() -> dict[str, int]:
    """Load whichever snapshot tables exist into SQLite (replacing current contents)."""
    counts = {}
    with connect() as conn:
        for table in SNAPSHOT_TABLES:
            path = settings.snapshot_dir / f"{table}.parquet"
            if not path.exists():
                continue
            df = pd.read_parquet(path)
            conn.execute(f"DELETE FROM {table}")
            if len(df):
                cols = ",".join(df.columns)
                marks = ",".join("?" * len(df.columns))
                rows = [tuple(None if pd.isna(v) else v for v in r) for r in df.itertuples(index=False)]
                conn.executemany(f"INSERT INTO {table} ({cols}) VALUES ({marks})", rows)
            counts[table] = len(df)
    return counts
