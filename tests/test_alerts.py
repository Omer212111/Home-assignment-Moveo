"""Alerts: monthly outlook (scheduled) and score changes (on a new scoring run)."""
from datetime import date

import pytest

from app import alerts, db
from app.config import load_alerts
from app.scoring import service, store


@pytest.fixture(scope="module", autouse=True)
def scores():
    service.load_results(restore=True)


@pytest.fixture
def posted(monkeypatch):
    """A fake webhook receiver."""
    calls = []
    monkeypatch.setattr(alerts.settings, "alert_webhook_url", "https://hooks.example/test")
    monkeypatch.setattr(alerts, "_post", lambda url, body: calls.append((url, body)))
    return calls


def test_outlook_month_uses_the_lead_time():
    lead = load_alerts().outlook.lead_days
    assert alerts.outlook_month(date(2026, 12, 31 - lead + 1)) == (2027, 1)   # within lead days of January
    assert alerts.outlook_month(date(2026, 12, 1)) == (2026, 12)


def test_outlook_applies_the_threshold_and_event_filter():
    jan = alerts.outlook_hubs(1)
    policy = load_alerts().outlook
    assert jan and all(h["index"] >= policy.min_index and h["past_events"] >= policy.min_events for h in jan)
    assert [h["index"] for h in jan] == sorted((h["index"] for h in jan), reverse=True)
    assert jan[0]["hub_id"] == "boston" and len(jan) == 17
    assert "nashville" not in {h["hub_id"] for h in alerts.outlook_hubs(9)}   # one flood is not a pattern
    assert alerts.outlook_hubs(5) == []                                      # May: no hub above threshold


def test_outlook_is_sent_once_per_month_and_delivered(posted):
    today = date(2027, 2, 24)                       # within lead days of March
    first = alerts.check_outlook(today)
    assert first is not None
    assert alerts.check_outlook(today) is None      # same month again: deduplicated, not re-sent
    assert len(posted) == 1
    url, body = posted[0]
    assert "March 2027" in body["text"] and body["alert"]["kind"] == "outlook"
    assert any(h["hub_id"] == "denver" for h in body["alert"]["hubs"])      # Denver's March peak
    a = next(a for a in alerts.recent() if a["alert_id"] == first)
    assert a["delivered_at"] and not a["delivery_error"]


def test_failed_delivery_is_recorded_retried_after_the_lease_then_given_up(monkeypatch):
    monkeypatch.setattr(alerts.settings, "alert_webhook_url", "https://hooks.example/down")
    def down(url, body):
        raise RuntimeError("receiver down")
    monkeypatch.setattr(alerts, "_post", down)
    alert_id = alerts.check_outlook(date(2027, 11, 28))           # December: winter hubs
    a = next(a for a in alerts.recent() if a["alert_id"] == alert_id)
    assert a["status"] == "pending" and a["delivered_at"] is None and "receiver down" in a["delivery_error"]
    assert alerts.deliver_pending() == 0 and a["attempts"] == 1   # lease still held: not retried yet
    for _ in range(alerts.MAX_ATTEMPTS):                           # leases expire -> retried
        with db.connect() as conn:
            conn.execute("UPDATE alerts SET lease_until = NULL WHERE alert_id = ?", (alert_id,))
        alerts.deliver_pending()
    a = next(a for a in alerts.recent() if a["alert_id"] == alert_id)
    assert a["status"] == "failed" and a["attempts"] == alerts.MAX_ATTEMPTS


def test_a_new_scoring_run_alerts_on_tier_changes_and_big_moves(posted):
    base = store.new_run()
    results = store.load_run(base)
    den = next(r for r in results if r.hub_id == "denver")      # +30 index points: alert
    den.index, den.total_days_per_year = den.index + 30, den.total_days_per_year + 1.455
    dal = next(r for r in results if r.hub_id == "dallas")      # +1 point but crosses a tier: alert
    dal.index, dal.tier = 150.5, "high"
    bos = next(r for r in results if r.hub_id == "boston")      # +5 points, same tier: no alert
    bos.index += 5
    sha, text = store.rules_identity()
    new = store.save_run(results, sha, text, store.data_fingerprint())   # triggers on_new_run
    changes = alerts.score_changes(base, new)
    assert {(c["hub_id"], c["hazard"]) for c in changes} == {("denver", "total"), ("dallas", "total")}
    body = posted[-1][1]
    assert body["alert"]["kind"] == "change" and "elevated -> high" in body["text"]
    with db.connect() as conn:                                  # the same pair is never alerted twice
        assert alerts.record_change_alert(conn, base, new) is None


def test_no_alert_when_nothing_moved(posted):
    a = store.new_run()
    n = len(posted)
    b = store.new_run()                                         # same data and rules
    assert alerts.score_changes(a, b) == [] and len(posted) == n


def test_api_lists_alerts_and_the_check_endpoint_is_protected(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(alerts.settings, "alerts_token", "secret")
    with TestClient(app) as c:
        assert c.post("/alerts/check").status_code == 401
        r = c.post("/alerts/check", headers={"X-Alerts-Token": "secret"}).json()
        assert "outlook_for" in r
        body = c.get("/alerts").json()
        assert isinstance(body["alerts"], list) and "webhook_configured" in body


def test_an_empty_outlook_is_stored_but_not_sent(posted):
    n = len(posted)
    alert_id = alerts.check_outlook(date(2027, 4, 30))          # May: no hub above threshold
    assert alert_id is not None and len(posted) == n             # recorded (month checked), not delivered
    assert alerts.check_outlook(date(2027, 4, 30)) is None



# ---------------------------------------------------------------- concurrency

def _run_threads(fn, n):
    import threading
    errors = []
    def wrapped():
        try:
            fn()
        except Exception as exc:
            errors.append(repr(exc))
    threads = [threading.Thread(target=wrapped) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_outlook_checks_create_and_send_exactly_once(posted):
    n = len(posted)
    assert _run_threads(lambda: alerts.check_outlook(date(2027, 12, 26)), 8) == []   # January 2028
    jan = [b for _, b in posted[n:] if b["alert"]["kind"] == "outlook" and b["alert"]["month"] == 1
           and b["alert"]["year"] == 2028]
    assert len(jan) == 1 and jan[0]["idempotency_key"] == "outlook:2028-01"


def test_concurrent_senders_never_post_the_same_alert_twice(posted, monkeypatch):
    import time
    def slow_post(url, body):          # a slow receiver widens the race window
        time.sleep(0.05)
        posted.append((url, body))
    monkeypatch.setattr(alerts, "_post", slow_post)
    monkeypatch.setattr(alerts.settings, "alert_webhook_url", None)
    ids = [alerts._record("outlook", f"race-test-{i}", None, "t", "m", {"month": 0, "year": 0}) for i in range(5)]
    with db.connect() as conn:          # stored while no webhook was configured; make them pending now
        conn.execute(f"UPDATE alerts SET status = 'pending' WHERE alert_id IN ({','.join('?' * 5)})", ids)
    monkeypatch.setattr(alerts.settings, "alert_webhook_url", "https://hooks.example/test")
    n = len(posted)
    assert _run_threads(alerts.deliver_pending, 6) == []
    sent = [b["alert"]["id"] for _, b in posted[n:]]
    assert sorted(sent) == sorted(ids)  # each alert exactly once, none lost


def test_an_alert_stored_before_a_crash_is_sent_later(posted, monkeypatch):
    """Outbox: storing and sending are separate, so a crash in between loses nothing."""
    alert_id = alerts._record("change", "crash-test", None, "t", "m", {})      # stored as pending...
    n = len(posted)                                                             # ...process "crashes" here
    alerts.deliver_pending()                                                   # next tick / next process
    assert [b["alert"]["id"] for _, b in posted[n:]] == [alert_id]


def test_simultaneous_startups_create_one_scoring_run_and_no_duplicate_alert(posted, monkeypatch):
    """Two servers start at once after the rules changed: both score, only one run is activated."""
    sha, text = store.rules_identity()
    monkeypatch.setattr(store, "rules_identity", lambda: ("rules-race-" + sha, text + "\n# race"))
    before = {r["run_id"] for r in store.list_runs()}
    assert _run_threads(store.ensure_current, 3) == []
    created = [r for r in store.list_runs() if r["run_id"] not in before]
    assert len(created) == 1 and created[0]["status"] == "active"


def test_service_state_is_swapped_atomically():
    """Run id and results always belong together, even while another run is being activated."""
    service.load_results(restore=False)
    run, results = service._state()
    assert store.load_run(run)[0].total_days_per_year == results[0].total_days_per_year
