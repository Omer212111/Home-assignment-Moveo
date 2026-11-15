"""Batch ingestion: public APIs -> raw cache -> SQLite -> parquet snapshot.

This runs offline, separate from the web server. The server only reads the
snapshot, so the demo never depends on third-party uptime or rate limits.

    uv run python -m app.ingest                      # everything, all hubs
    uv run python -m app.ingest --source declarations
    uv run python -m app.ingest --source weather --hubs denver,miami
"""
import argparse
import json
from datetime import datetime, timezone

import pandas as pd

from app import db
from app.config import load_hubs
from app.settings import settings
from app.sources import fema_declarations, open_meteo
from app.sources.http_cache import SourceError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log_run(conn, source: str, params: dict, started: str, rows: int, status: str, detail: str = ""):
    conn.execute(
        "INSERT INTO ingest_runs (source, params, started_at, finished_at, rows, status, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (source, json.dumps(params), started, _now(), rows, status, detail[:500]),
    )


def _upsert(conn, table: str, df: pd.DataFrame):
    if df.empty:
        return
    cols = ",".join(df.columns)
    marks = ",".join("?" * len(df.columns))
    rows = [tuple(None if pd.isna(v) else v for v in r) for r in df.itertuples(index=False)]
    conn.executemany(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", rows)


def ingest_hubs(conn, hubs):
    df = pd.DataFrame([{"hub_id": h.id, "city": h.city, "state": h.state, "lat": h.lat, "lon": h.lon,
                        "county_fips": h.county_fips, "region": h.region, "pop_rank": h.pop_rank} for h in hubs])
    conn.execute("DELETE FROM hubs")
    _upsert(conn, "hubs", df)


def ingest_weather(conn, hubs, start: str, end: str, refresh: bool) -> list[str]:
    failures = []
    for i, hub in enumerate(hubs, 1):
        started = _now()
        params = {"hub_id": hub.id, "start": start, "end": end}
        try:
            df, path, from_cache = open_meteo.fetch_daily(hub, start, end, refresh=refresh)
            _upsert(conn, "daily_weather", df)
            _log_run(conn, "open_meteo", params, started, len(df), "ok",
                     f"{'cache' if from_cache else 'api'}: {path}")
            print(f"[weather {i}/{len(hubs)}] {hub.id}: {len(df)} days ({'cache' if from_cache else 'api'})")
        except (SourceError, ValueError, OSError) as exc:
            _log_run(conn, "open_meteo", params, started, 0, "error", str(exc))
            failures.append(hub.id)
            print(f"[weather {i}/{len(hubs)}] {hub.id}: FAILED - {exc}")
            if "limit" in str(exc).lower():
                print("Rate limit reached: stopping weather ingest (cached hubs are kept).")
                break
        conn.commit()  # keep progress if a later hub fails
    return failures


def ingest_declarations(conn, hubs, since: str, refresh: bool) -> list[str]:
    failures = []
    counties = sorted({h.county_fips for h in hubs})
    for i, fips in enumerate(counties, 1):
        started = _now()
        params = {"county_fips": fips, "since": since}
        try:
            df, path, from_cache = fema_declarations.fetch_county(fips, since, refresh=refresh)
            conn.execute("DELETE FROM disaster_declarations WHERE county_fips = ?", (fips,))
            _upsert(conn, "disaster_declarations", df)
            _log_run(conn, "fema_declarations", params, started, len(df), "ok",
                     f"{'cache' if from_cache else 'api'}: {path}")
            print(f"[declarations {i}/{len(counties)}] {fips}: {len(df)} disasters")
        except (SourceError, ValueError, OSError) as exc:
            _log_run(conn, "fema_declarations", params, started, 0, "error", str(exc))
            failures.append(fips)
            print(f"[declarations {i}/{len(counties)}] {fips}: FAILED - {exc}")
    return failures


def print_status():
    """What is in the database right now, per source and per hub."""
    with db.connect() as conn:
        n_hubs = conn.execute("SELECT COUNT(*) FROM hubs").fetchone()[0]
        wx = conn.execute("SELECT COUNT(DISTINCT hub_id), COUNT(*), MIN(date), MAX(date) FROM daily_weather").fetchone()
        dec = conn.execute("SELECT COUNT(DISTINCT county_fips), COUNT(*), MIN(declaration_date), "
                           "MAX(declaration_date) FROM disaster_declarations").fetchone()
        missing_wx = [r[0] for r in conn.execute(
            "SELECT hub_id FROM hubs WHERE hub_id NOT IN (SELECT DISTINCT hub_id FROM daily_weather) ORDER BY pop_rank")]
    print(f"hubs: {n_hubs}")
    print(f"daily_weather: {wx[1]} rows for {wx[0]}/{n_hubs} hubs, {wx[2]} .. {wx[3]}")
    print(f"disaster_declarations: {dec[1]} disasters in {dec[0]} counties, {dec[2]} .. {dec[3]}")
    if missing_wx:
        print(f"hubs without weather yet ({len(missing_wx)}): {', '.join(missing_wx)}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=["all", "weather", "declarations"], default="all")
    p.add_argument("--hubs", help="comma-separated hub ids (default: all)")
    p.add_argument("--start", default=settings.history_start)
    p.add_argument("--end", default=settings.history_end)
    p.add_argument("--refresh", action="store_true", help="ignore the raw cache and call the APIs again")
    p.add_argument("--no-snapshot", action="store_true", help="do not export the parquet snapshot")
    p.add_argument("--status", action="store_true", help="only show what is in the database, download nothing")
    args = p.parse_args(argv)

    if args.status:
        db.restore_snapshot()
        print_status()
        return

    hubs = load_hubs()
    if args.hubs:
        wanted = set(args.hubs.split(","))
        unknown = wanted - {h.id for h in hubs}
        if unknown:
            p.error(f"unknown hub ids: {sorted(unknown)}")
        hubs = [h for h in hubs if h.id in wanted]

    # Start from the existing snapshot so a partial run adds to it instead of losing data.
    db.restore_snapshot()
    failures = []
    with db.connect() as conn:
        ingest_hubs(conn, load_hubs())
        if args.source in ("all", "declarations"):
            failures += ingest_declarations(conn, hubs, settings.declarations_since, args.refresh)
        if args.source in ("all", "weather"):
            failures += ingest_weather(conn, hubs, args.start, args.end, args.refresh)

    print_status()
    if not args.no_snapshot:
        print("snapshot:", db.export_snapshot())
    if failures:
        print(f"WARNING: {len(failures)} failures: {failures}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
