"""Alerts: the monthly risk outlook and score-change alerts.

1. Outlook (scheduled): `lead_days` before a month starts, one digest of the hubs for which that
   month has historically been severe (monthly index >= min_index, >= min_events past events).
2. Change (event-driven): when a new scoring run becomes active, the hubs whose index crossed a
   tier or moved by >= min_index_change points, for the total and per hazard.

Every alert is stored in the `alerts` table with a unique dedup key, so running a check twice (two
servers, a restart, a retry) never sends twice. Delivery is a webhook POST in Slack-compatible
JSON ({"text": ...}) to ALERT_WEBHOOK_URL; without it, alerts are stored and shown at GET /alerts.

Policy: config/alerts.yaml. Triggers: an hourly check in the server (with a catch-up at startup),
POST /alerts/check (webhook-triggered, e.g. from cron), and every new scoring run.

    uv run python -m app.alerts list
    uv run python -m app.alerts outlook [--month 2027-01]
"""
import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app import db
from app.config import load_alerts, load_scoring
from app.settings import settings

log = logging.getLogger("weather_agent.alerts")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]
MONTH_ABBR = [m[:3] for m in MONTHS]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- storage and delivery (outbox)
#
# Concurrency model (several senders may run at once: the server loop, POST /alerts/check, the CLI,
# ingestion, a second server):
# - Creation is atomic: INSERT OR IGNORE on a UNIQUE dedup key. Exactly one caller creates a given
#   alert; the others get None and send nothing.
# - Delivery is an outbox: alerts are stored as 'pending' and a sender CLAIMS one with a single
#   UPDATE ... RETURNING that sets a lease. Only the claimer posts it; a crashed sender's lease
#   expires and another sender retries. Nothing is lost between "stored" and "sent".
# - HTTP cannot be exactly-once (the receiver may get the POST and the reply may be lost), so
#   delivery is at-least-once and every POST carries the dedup key as an idempotency key.

LEASE_SECONDS = 120
MAX_ATTEMPTS = 5


def _record(kind: str, dedup_key: str, run_id: int | None, title: str, message: str, payload: dict,
            send: bool = True, conn=None) -> int | None:
    """Store an alert; returns its id, or None if an alert with this dedup key already exists.

    send=False (or no webhook configured) stores it as 'stored': visible, never posted.
    conn: write inside the caller's transaction (e.g. together with a scoring run's activation)."""
    status = "pending" if send and settings.alert_webhook_url else "stored"
    sql = ("INSERT OR IGNORE INTO alerts (kind, dedup_key, run_id, title, message, payload_json, created_at, status) "
           "VALUES (?,?,?,?,?,?,?,?)")
    args = (kind, dedup_key, run_id, title, message, json.dumps(payload), _now(), status)
    if conn is not None:
        cur = conn.execute(sql, args)
        return cur.lastrowid if cur.rowcount else None
    with db.connect() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid if cur.rowcount else None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=8), reraise=True)
def _post(url: str, body: dict) -> None:
    httpx.post(url, json=body, timeout=10).raise_for_status()


def _claim() -> dict | None:
    """Atomically take one pending alert whose lease is free (or expired)."""
    now = datetime.now(timezone.utc)
    until = (now + timedelta(seconds=LEASE_SECONDS)).isoformat(timespec="seconds")
    with db.connect() as conn:
        row = conn.execute(
            "UPDATE alerts SET lease_until = ?, attempts = attempts + 1 WHERE alert_id = ("
            "  SELECT alert_id FROM alerts WHERE status = 'pending' "
            "  AND (lease_until IS NULL OR lease_until < ?) ORDER BY alert_id LIMIT 1) "
            "RETURNING *", (until, now.isoformat(timespec="seconds"))).fetchone()
    return dict(row) if row else None


def deliver_pending(max_alerts: int = 20) -> int:
    """Send pending alerts (claim -> POST -> record). Safe to call from many places at once."""
    if not settings.alert_webhook_url:
        return 0
    sent = 0
    for _ in range(max_alerts):
        a = _claim()
        if a is None:
            break
        try:
            _post(settings.alert_webhook_url, {
                "text": f"*{a['title']}*\n{a['message']}",
                "idempotency_key": a["dedup_key"],   # lets the receiver drop a re-delivery
                "alert": {"id": a["alert_id"], "kind": a["kind"], **json.loads(a["payload_json"])}})
            with db.connect() as conn:
                conn.execute("UPDATE alerts SET status = 'delivered', delivered_at = ?, delivery_error = NULL, "
                             "lease_until = NULL WHERE alert_id = ?", (_now(), a["alert_id"]))
            sent += 1
        except Exception as exc:  # recorded, never raised into scoring or the API; retried after the lease
            error = f"{type(exc).__name__}: {exc}"[:500]
            final = a["attempts"] >= MAX_ATTEMPTS
            with db.connect() as conn:
                conn.execute("UPDATE alerts SET delivery_error = ?, status = ? WHERE alert_id = ?",
                             (error, "failed" if final else "pending", a["alert_id"]))
            log.warning("alert %s delivery failed (attempt %s): %s", a["alert_id"], a["attempts"], error)
    return sent


def recent(limit: int = 20) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM alerts ORDER BY alert_id DESC LIMIT ?", (limit,)).fetchall()
    return [{**{k: r[k] for k in r.keys() if k != "payload_json"}, "payload": json.loads(r["payload_json"])}
            for r in rows]


# ---------------------------------------------------------------- 1. monthly outlook

def outlook_month(today: date | None = None) -> tuple[int, int]:
    """The month the outlook is for: the month starting within `lead_days` (else the current one)."""
    today = today or date.today()
    target = today + timedelta(days=load_alerts().outlook.lead_days)
    return target.year, target.month


def outlook_hubs(month: int) -> list[dict]:
    """Hubs whose history makes this month severe, highest index first."""
    from app.scoring import service
    from app.scoring.periods import breakdown
    policy = load_alerts().outlook
    out = []
    for r in service.results():
        if not r.weather_complete:
            continue
        m = breakdown(r, "total", "month", [])[month - 1]
        if m["index"] >= policy.min_index and m["events_in_data"] >= policy.min_events:
            out.append({"hub_id": r.hub_id, "city": r.city, "state": r.state, "index": m["index"],
                        "tier": m["tier"], "expected_days": m["days_per_year"],
                        "main_hazard": m["main_hazard"], "past_events": m["events_in_data"]})
    return sorted(out, key=lambda h: -h["index"])


def check_outlook(today: date | None = None, force: bool = False) -> int | None:
    """Create (and deliver) the outlook digest for the coming month, once. Returns the alert id."""
    from app.scoring import service
    today = today or date.today()
    year, month = outlook_month(today)
    hubs = outlook_hubs(month)
    policy = load_alerts().outlook
    labels = load_scoring().hazards
    run = service.run_id()
    key = f"outlook:{year}-{month:02d}" + (f":forced:{_now()}" if force else "")
    late = today >= date(year, month, 1)  # catch-up after downtime: the month has already started
    title = (f"Weather risk outlook for {MONTHS[month - 1]} {year}: "
             + (f"{len(hubs)} hub(s) above threshold" if hubs else "no hub above threshold (not sent)")
             + (" (sent late: the month has started)" if late else ""))
    if hubs:
        from app.agent.tools import COASTAL_WIND_HUBS
        lines = [f"- {h['city']}, {h['state']}: index {h['index']:.0f} ({h['tier']}), "
                 f"{h['expected_days']:.2f} expected disruption days, mostly {labels.get(h['main_hazard'], h['main_hazard'])}"
                 f" ({h['past_events']} past events)"
                 + (" [wind likely overstated: coastal grid cell]"
                    if h["hub_id"] in COASTAL_WIND_HUBS and h["main_hazard"] == "high_wind" else "")
                 for h in hubs]
    else:
        lines = ["No hub has historically exceeded the threshold in this month."]
    message = "\n".join([
        f"Historically severe {MONTHS[month - 1]} for these hubs (monthly index >= {policy.min_index:g}, "
        f"i.e. at least {policy.min_index / 100:g}x a typical hub's average month, with >= {policy.min_events} past events):",
        *lines,
        f"Based on scoring run {run} (weather {settings.history_start[:4]}-{settings.history_end[:4]}, "
        f"FEMA declarations 20 years). Historical averages, not a forecast."])
    # An empty outlook is stored (the month counts as checked) but not sent: a monthly "nothing to
    # report" message is noise that teaches people to ignore the channel.
    alert_id = _record("outlook", key, run, title, message,
                       {"year": year, "month": month, "hubs": hubs, "policy": policy.model_dump()}, send=bool(hubs))
    deliver_pending()
    return alert_id


# ---------------------------------------------------------------- 2. score changes

def score_changes(old_run: int, new_run: int, conn=None) -> list[dict]:
    """Hub totals and hazards whose index crossed a tier or moved by >= min_index_change."""
    policy = load_alerts().change
    if conn is None:
        with db.connect() as c:
            return score_changes(old_run, new_run, c)
    rows = conn.execute(
            "SELECT n.hub_id, 'total' AS hazard, o.index_value AS old_index, n.index_value AS new_index, "
            "o.tier AS old_tier, n.tier AS new_tier, o.total_days_per_year AS old_days, "
            "n.total_days_per_year AS new_days FROM hub_results n "
            "JOIN hub_results o ON o.hub_id = n.hub_id AND o.run_id = ? WHERE n.run_id = ? "
            "UNION ALL "
            "SELECT n.hub_id, n.hazard, o.index_value, n.index_value, o.tier, n.tier, o.days_per_year, "
            "n.days_per_year FROM hazard_scores n "
            "JOIN hazard_scores o ON o.hub_id = n.hub_id AND o.hazard = n.hazard AND o.run_id = ? "
            "WHERE n.run_id = ?", (old_run, new_run, old_run, new_run)).fetchall()
    out = []
    for r in rows:
        delta = round(r["new_index"] - r["old_index"], 1)
        tier_moved = r["old_tier"] != r["new_tier"]
        if abs(delta) >= policy.min_index_change or (policy.tier_change and tier_moved):
            out.append({**dict(r), "index_change": delta, "tier_changed": tier_moved})
    return sorted(out, key=lambda c: (c["hazard"] != "total", -abs(c["index_change"])))


def record_change_alert(conn, old_run: int | None, new_run: int) -> int | None:
    """Called inside the transaction that activates `new_run` (store.save_run): the alert is stored
    together with the activation, so a run can never become active without its alert. Delivery
    happens after the commit (deliver_pending)."""
    if old_run is None or old_run == new_run:
        return None
    changes = score_changes(old_run, new_run, conn)
    if not changes:
        return None
    labels = {**load_scoring().hazards, "total": "all hazards"}
    lines = [f"- {c['hub_id']} ({labels.get(c['hazard'], c['hazard'])}): index {c['old_index']:.0f} -> "
             f"{c['new_index']:.0f} ({c['index_change']:+.0f})"
             + (f", tier {c['old_tier']} -> {c['new_tier']}" if c["tier_changed"] else "")
             + f"; {c['old_days']:.2f} -> {c['new_days']:.2f} days/yr" for c in changes[:25]]
    hubs = sorted({c["hub_id"] for c in changes})
    title = f"Risk score changes in scoring run {new_run}: {len(hubs)} hub(s)"
    message = "\n".join([f"Compared with scoring run {old_run} (new data or new rules):", *lines,
                         *([f"... and {len(changes) - 25} more"] if len(changes) > 25 else [])])
    return _record("change", f"change:{old_run}->{new_run}", new_run, title, message,
                   {"old_run": old_run, "new_run": new_run, "changes": changes}, conn=conn)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="recent alerts")
    o = sub.add_parser("outlook", help="create the outlook for the coming month (once)")
    o.add_argument("--month", help="YYYY-MM: preview the outlook for this month (sent as a forced alert)")
    args = p.parse_args(argv)
    from app.scoring import service
    service.load_results(restore=True)
    if args.cmd == "outlook":
        if args.month:
            y, m = map(int, args.month.split("-"))
            alert = check_outlook(date(y, m, 1) - timedelta(days=load_alerts().outlook.lead_days), force=True)
        else:
            alert = check_outlook()
        print(f"alert {alert}" if alert else "outlook for this month already sent")
    for a in recent(5 if args.cmd == "outlook" else 20):
        print(f"\n#{a['alert_id']} [{a['kind']}] {a['created_at']}  delivered: {a['delivered_at'] or 'no'}\n"
              f"{a['title']}\n{a['message']}")


if __name__ == "__main__":
    main(sys.argv[1:])
