"""Phase 3: webhooks (outbox), conflict events, provenance review_state."""

import json

import pytest
from rip.ingest import ingest_profile
from rip.models import ChangeLog, WebhookDelivery
from rip.webhooks import (
    EVENT_CONFLICT,
    EVENT_PERSON_UPDATED,
    create_subscription,
    deliver_pending,
    sign,
    validate_url,
)
from tests.test_ingest import github_profile


def _sub(session, url="https://hooks.example.com/rip", events=None, monkeypatch=None):
    """Create a subscription, bypassing DNS validation."""
    import rip.webhooks as wh

    original = wh.validate_url
    wh.validate_url = lambda u: u
    try:
        return create_subscription(session, url, events)
    finally:
        wh.validate_url = original


def test_signature_covers_exact_bytes():
    body = json.dumps({"b": 2, "a": 1}, sort_keys=True).encode()
    sig = sign("secret", body)
    assert sig.startswith("sha256=")
    assert sign("secret", body) == sig  # deterministic
    assert sign("other", body) != sig


def test_ssrf_urls_rejected():
    for bad in ["http://localhost/hook", "http://127.0.0.1/x",
                "http://169.254.169.254/latest", "ftp://example.com/x"]:
        with pytest.raises(ValueError):
            validate_url(bad)


def test_ingest_queues_deliveries(session):
    sub, secret = _sub(session)
    ingest_profile(session, github_profile())
    deliveries = session.query(WebhookDelivery).all()
    assert deliveries
    assert all(d.subscription_id == sub.id for d in deliveries)
    assert all(d.status == "pending" for d in deliveries)
    payload = deliveries[0].payload
    assert payload["event_type"] == EVENT_PERSON_UPDATED
    assert payload["person_id"] and payload["sequence_id"]


def test_no_subscriptions_means_no_deliveries(session):
    ingest_profile(session, github_profile())
    assert session.query(WebhookDelivery).count() == 0


def test_event_type_filter_respected(session):
    _sub(session, events=[EVENT_CONFLICT])
    ingest_profile(session, github_profile())
    # only person.updated changes so far -> nothing queued for a conflict-only sub
    assert session.query(WebhookDelivery).count() == 0


def test_conflict_emits_change_event_and_delivery(session):
    _sub(session, events=[EVENT_CONFLICT])
    ingest_profile(session, github_profile())
    moved = github_profile()
    moved.source = "orcid"
    moved.external_id = "0000-9"
    moved.url = "https://orcid.org/0000-9"
    moved.usernames = []
    moved.location = "Zurich, Switzerland"
    moved.raw = {"v": 2}
    ingest_profile(session, moved)

    events = session.query(ChangeLog).filter(
        ChangeLog.field == "conflict_detected:location"
    ).all()
    assert events
    assert "github" in events[0].new_value and "orcid" in events[0].new_value
    deliveries = session.query(WebhookDelivery).all()
    assert deliveries and deliveries[0].event_type == EVENT_CONFLICT


def test_delivery_success_and_signature(session, monkeypatch):
    sub, secret = _sub(session)
    ingest_profile(session, github_profile())
    seen = {}

    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, content=None, headers=None):
            seen["url"], seen["body"], seen["headers"] = url, content, headers
            return FakeResp()

    monkeypatch.setattr("rip.webhooks.httpx.Client", FakeClient)
    delivered, failed = deliver_pending(session)
    assert delivered > 0 and failed == 0
    assert seen["headers"]["X-RIP-Signature"] == sign(secret, seen["body"])
    assert all(d.status == "delivered" for d in session.query(WebhookDelivery))


def test_delivery_retries_then_fails(session, monkeypatch):
    _sub(session)
    ingest_profile(session, github_profile())

    class FakeResp:
        status_code = 500

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, content=None, headers=None):
            return FakeResp()

    monkeypatch.setattr("rip.webhooks.httpx.Client", FakeClient)
    monkeypatch.setattr("rip.webhooks.time.sleep", lambda s: None)
    for _ in range(3):
        deliver_pending(session)
    statuses = {d.status for d in session.query(WebhookDelivery)}
    assert statuses == {"failed"}
    assert all(d.attempts == d.max_attempts for d in session.query(WebhookDelivery))


def test_deactivated_subscription_stops_delivery(session):
    sub, _ = _sub(session)
    ingest_profile(session, github_profile())
    sub.is_active = False
    session.commit()
    delivered, failed = deliver_pending(session)
    assert delivered == 0 and failed > 0


def test_provenance_exposes_review_state(session):
    from rip.api import get_provenance

    person = ingest_profile(session, github_profile())
    data = get_provenance(person.id, db=session)
    assert data["sources"][0]["review_state"] == "unreviewed"


def test_health_reports_backlog(session):
    from rip.webhooks import health

    empty = health(session)
    assert empty == {"active_subscriptions": 0, "pending": 0, "delivered": 0,
                     "failed": 0, "last_delivery_at": None, "oldest_pending_at": None}

    sub, _ = _sub(session)
    ingest_profile(session, github_profile())
    state = health(session)
    assert state["active_subscriptions"] == 1
    assert state["pending"] > 0
    assert state["oldest_pending_at"] is not None
    assert state["last_delivery_at"] is None


def test_health_after_successful_delivery(session, monkeypatch):
    from rip.webhooks import health

    _sub(session)
    ingest_profile(session, github_profile())

    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, content=None, headers=None): return FakeResp()

    monkeypatch.setattr("rip.webhooks.httpx.Client", FakeClient)
    deliver_pending(session)
    state = health(session)
    assert state["pending"] == 0 and state["delivered"] > 0
    assert state["last_delivery_at"] is not None


def test_health_endpoint_shape(session):
    from rip.api import webhook_health

    _sub(session)
    ingest_profile(session, github_profile())
    data = webhook_health(db=session)
    assert set(data) == {"active_subscriptions", "pending", "delivered", "failed",
                         "last_delivery_at", "oldest_pending_at"}
