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
CREATE TABLE IF NOT EXISTS hub_scores (
    hub_id TEXT, hazard TEXT, score REAL, tier TEXT, breakdown_json TEXT,
    scoring_version TEXT, data_as_of TEXT, computed_at TEXT,
    PRIMARY KEY (hub_id, hazard, scoring_version, computed_at)
);
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, params TEXT,
    started_at TEXT, finished_at TEXT, rows INTEGER, status TEXT, detail TEXT
);
"""

# Tables exported to / restored from the committed snapshot.
SNAPSHOT_TABLES = ["hubs", "daily_weather", "disaster_declarations", "ingest_runs"]


@contextmanager
def connect():
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
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
