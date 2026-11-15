"""Scores in the database: scoring runs, per-hub results, per-hazard scores, disruption days.

The scoring engine (disruption.py) computes; this module stores and loads. A scoring run records
the exact rules (rule_sets) and data (a fingerprint of the snapshot) it used, so every score is
reproducible, and two runs can be compared ("did a hub's risk change?").

    uv run python -m app.scoring.store             # list runs
    uv run python -m app.scoring.store --new       # score again now (e.g. after editing the rules)
    uv run python -m app.scoring.store --diff A B  # what changed between two runs
"""
import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone

from app import db
from app.config import load_scoring
from app.scoring.disruption import (CountedDeclaration, DayRecord, Driver, HazardDisruption, HubDisruption,
                                    compute_all)
from app.settings import settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rules_identity() -> tuple[str, str]:
    """(sha256, yaml text) of the scoring rules as they are now."""
    text = settings.scoring_file.read_text(encoding="utf-8")
    return hashlib.sha256(text.encode()).hexdigest(), text


def data_fingerprint() -> str:
    """Hash of the snapshot files the scores are computed from (changes when ingestion runs)."""
    h = hashlib.sha256()
    for table in db.SNAPSHOT_TABLES:
        if table == "ingest_runs":  # the download log does not change the scores
            continue
        path = settings.snapshot_dir / f"{table}.parquet"
        h.update(table.encode())
        h.update(path.read_bytes() if path.exists() else b"")
    return h.hexdigest()


def active_run() -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM scoring_runs WHERE status = 'active' "
                           "ORDER BY run_id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def active_run_id() -> int | None:
    with db.connect() as conn:
        row = conn.execute("SELECT MAX(run_id) FROM scoring_runs WHERE status = 'active'").fetchone()
    return row[0]


def _compute() -> list[HubDisruption]:
    window = (date.fromisoformat(settings.history_start), date.fromisoformat(settings.history_end))
    return compute_all(db.read_df("SELECT * FROM daily_weather"),
                       db.read_df("SELECT * FROM disaster_declarations"),
                       db.read_df("SELECT * FROM hubs ORDER BY pop_rank"), load_scoring(), window)


def save_run(results: list[HubDisruption], rules_sha: str, rules_yaml: str, fingerprint: str) -> int:
    """Write a complete run, then make it the active one in a single transaction.

    Readers see either the previous active run or the new one, never a half-written run."""
    data_as_of = max((r.weather_period.split("..")[1] for r in results if r.weather_period), default=None)
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO rule_sets (sha256, rules_yaml, first_used_at) VALUES (?,?,?)",
                     (rules_sha, rules_yaml, _now()))
        rule_set_id = conn.execute("SELECT rule_set_id FROM rule_sets WHERE sha256 = ?", (rules_sha,)).fetchone()[0]
        run_id = conn.execute(
            "INSERT INTO scoring_runs (rule_set_id, data_fingerprint, data_as_of, status, hubs, started_at) "
            "VALUES (?,?,?,?,?,?)", (rule_set_id, fingerprint, data_as_of, "running", len(results), _now())).lastrowid
        conn.executemany(
            "INSERT INTO hub_results VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(run_id, r.hub_id, r.total_days_per_year, int(r.weather_complete), r.weather_coverage, r.weather_period,
              r.weather_years, r.declaration_years, r.weather_days_deduplicated,
              json.dumps([c.model_dump() for c in r.declarations_counted])) for r in results])
        conn.executemany(
            "INSERT INTO hazard_scores VALUES (?,?,?,?,?,?,?,?,?)",
            [(run_id, r.hub_id, h.hazard, h.label, h.days_per_year, h.from_weather, h.from_declarations,
              h.events_per_year, json.dumps([d.model_dump() for d in h.drivers]))
             for r in results for h in r.hazards.values()])
        conn.executemany(
            "INSERT INTO disruption_days VALUES (?,?,?,?,?,?,?,?)",
            [(run_id, r.hub_id, x.date.isoformat(), x.hazard, x.value, x.channel, x.label, x.event)
             for r in results for x in r.records])
        # Switch the active run atomically (same transaction as the inserts above).
        conn.execute("UPDATE scoring_runs SET status = 'superseded' WHERE status = 'active'")
        conn.execute("UPDATE scoring_runs SET status = 'active', finished_at = ? WHERE run_id = ?", (_now(), run_id))
    return run_id


def load_run(run_id: int) -> list[HubDisruption]:
    """Rebuild the engine's objects from the tables of one run."""
    with db.connect() as conn:
        hubs = {r["hub_id"]: dict(r) for r in conn.execute("SELECT * FROM hubs ORDER BY pop_rank")}
        results = {r["hub_id"]: dict(r) for r in conn.execute("SELECT * FROM hub_results WHERE run_id = ?", (run_id,))}
        hazards: dict[str, dict] = {}
        for r in conn.execute("SELECT * FROM hazard_scores WHERE run_id = ?", (run_id,)):
            hazards.setdefault(r["hub_id"], {})[r["hazard"]] = HazardDisruption(
                hazard=r["hazard"], label=r["label"], days_per_year=r["days_per_year"],
                from_weather=r["from_weather"], from_declarations=r["from_declarations"],
                events_per_year=r["events_per_year"],
                drivers=[Driver(**d) for d in json.loads(r["drivers_json"])])
        records: dict[str, list] = {}
        for r in conn.execute("SELECT * FROM disruption_days WHERE run_id = ? ORDER BY hub_id, date", (run_id,)):
            records.setdefault(r["hub_id"], []).append(DayRecord(
                date=date.fromisoformat(r["date"]), hazard=r["hazard"], value=r["value"], channel=r["channel"],
                label=r["label"], event=r["event"]))
    out = []
    for hub_id, hub in hubs.items():  # population order, as the engine returns them
        res = results.get(hub_id)
        if res is None:
            continue
        out.append(HubDisruption(
            hub_id=hub_id, city=hub["city"], state=hub["state"], region=hub["region"],
            total_days_per_year=res["total_days_per_year"], hazards=hazards.get(hub_id, {}),
            weather_complete=bool(res["weather_complete"]), weather_coverage=res["weather_coverage"],
            weather_period=res["weather_period"], weather_days_deduplicated=res["weather_days_deduplicated"],
            declarations_counted=[CountedDeclaration(**c) for c in json.loads(res["declarations_json"])],
            records=records.get(hub_id, []), weather_years=res["weather_years"],
            declaration_years=res["declaration_years"]))
    return out


def ensure_current() -> int:
    """The active run if it matches the current rules and data; otherwise score now and store a new run."""
    rules_sha, rules_yaml = rules_identity()
    fingerprint = data_fingerprint()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT r.run_id FROM scoring_runs r JOIN rule_sets s ON s.rule_set_id = r.rule_set_id "
            "WHERE r.status = 'active' AND s.sha256 = ? AND r.data_fingerprint = ?",
            (rules_sha, fingerprint)).fetchone()
    if row:
        return row[0]
    return save_run(_compute(), rules_sha, rules_yaml, fingerprint)


def new_run() -> int:
    """Score now and store a new active run, even if nothing changed."""
    rules_sha, rules_yaml = rules_identity()
    return save_run(_compute(), rules_sha, rules_yaml, data_fingerprint())


def diff_runs(old: int, new: int, threshold: float = 0.1) -> list[dict]:
    """Hubs whose total or a hazard moved by at least `threshold` days per year between two runs."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT n.hub_id, n.hazard, o.days_per_year AS old, n.days_per_year AS new "
            "FROM hazard_scores n JOIN hazard_scores o ON o.hub_id = n.hub_id AND o.hazard = n.hazard "
            "WHERE n.run_id = ? AND o.run_id = ? "
            "UNION ALL "
            "SELECT n.hub_id, 'total', o.total_days_per_year, n.total_days_per_year "
            "FROM hub_results n JOIN hub_results o ON o.hub_id = n.hub_id WHERE n.run_id = ? AND o.run_id = ?",
            (new, old, new, old)).fetchall()
    changes = [{"hub_id": r["hub_id"], "hazard": r["hazard"], "old": r["old"], "new": r["new"],
                "change": round(r["new"] - r["old"], 3)} for r in rows if abs(r["new"] - r["old"]) >= threshold]
    return sorted(changes, key=lambda c: -abs(c["change"]))


def list_runs() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT r.run_id, r.status, substr(s.sha256, 1, 10) AS rules, substr(r.data_fingerprint, 1, 10) AS data, "
            "r.data_as_of, r.hubs, r.started_at, r.finished_at FROM scoring_runs r "
            "JOIN rule_sets s ON s.rule_set_id = r.rule_set_id ORDER BY r.run_id")]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--new", action="store_true", help="score now and make it the active run")
    p.add_argument("--diff", nargs=2, type=int, metavar=("OLD", "NEW"), help="changes between two runs")
    args = p.parse_args(argv)
    db.restore_snapshot()
    if args.new:
        print(f"new active run: {new_run()}")
    elif args.diff:
        changes = diff_runs(*args.diff)
        print(f"{len(changes)} changes of 0.1 days/yr or more between run {args.diff[0]} and run {args.diff[1]}")
        for c in changes[:30]:
            print(f"  {c['hub_id']:<16} {c['hazard']:<13} {c['old']:>7.2f} -> {c['new']:>7.2f}  ({c['change']:+.2f})")
        return
    for r in list_runs():
        print(f"run {r['run_id']:>3}  {r['status']:<10}  rules {r['rules']}  data {r['data']}  "
              f"as of {r['data_as_of']}  {r['hubs']} hubs  {r['started_at']}")


if __name__ == "__main__":
    main(sys.argv[1:])
