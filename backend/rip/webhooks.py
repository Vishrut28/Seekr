"""Webhook fan-out (outbox pattern, no queue broker).

Deliveries are *written* in the same transaction as the change they describe,
then *sent* later by `rip.cli deliver-webhooks`. A subscriber therefore never
hears about a change that was rolled back, and ingest latency never depends on
subscriber uptime.

The signature covers the exact bytes sent — the payload is serialized once and
that same buffer is both signed and posted, so a receiver can verify without
guessing our JSON formatting.
"""

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from ipaddress import ip_address
from socket import gethostbyname
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ChangeLog, WebhookDelivery, WebhookSubscription

logger = logging.getLogger("rip.webhooks")

EVENT_PERSON_UPDATED = "person.updated"
EVENT_CONFLICT = "person.conflict"
DEFAULT_EVENTS = [EVENT_PERSON_UPDATED]
TIMEOUT = 10.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_url(url: str) -> str:
    """Reject anything that would make the delivery worker a confused deputy."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("webhook url must be http or https")
    if not parsed.hostname:
        raise ValueError("webhook url has no host")
    try:
        resolved = ip_address(gethostbyname(parsed.hostname))
    except Exception as exc:
        raise ValueError(f"webhook host does not resolve: {exc}") from exc
    if (
        resolved.is_private or resolved.is_loopback or resolved.is_link_local
        or resolved.is_reserved or resolved.is_multicast
    ):
        raise ValueError("webhook url resolves to a non-public address")
    return url


def create_subscription(
    session: Session, url: str, event_types: list[str] | None = None,
    description: str | None = None,
) -> tuple[WebhookSubscription, str]:
    """Returns (subscription, plaintext signing secret shown once)."""
    validate_url(url)
    secret = secrets.token_urlsafe(32)
    sub = WebhookSubscription(
        url=url,
        event_types=event_types or list(DEFAULT_EVENTS),
        signing_secret=secret,
        description=description,
    )
    session.add(sub)
    session.commit()
    return sub, secret


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _event_type_for(change: ChangeLog) -> str:
    return EVENT_CONFLICT if change.field.startswith("conflict") else EVENT_PERSON_UPDATED


def enqueue_for_changes(session: Session, changes: list[ChangeLog]) -> int:
    """Queue one delivery per (active subscription, matching change)."""
    if not changes:
        return 0
    subs = session.execute(
        select(WebhookSubscription).where(WebhookSubscription.is_active.is_(True))
    ).scalars().all()
    if not subs:
        return 0
    queued = 0
    for change in changes:
        event_type = _event_type_for(change)
        for sub in subs:
            if event_type not in (sub.event_types or DEFAULT_EVENTS):
                continue
            session.add(
                WebhookDelivery(
                    subscription_id=sub.id,
                    event_type=event_type,
                    payload={
                        "event_type": event_type,
                        "sequence_id": change.id,
                        "person_id": change.person_id,
                        "field": change.field,
                        "old_value": change.old_value,
                        "new_value": change.new_value,
                        "occurred_at": change.changed_at.isoformat()
                        if change.changed_at else None,
                    },
                )
            )
            queued += 1
    return queued


def health(session: Session) -> dict:
    """Backlog visibility: deliveries only move when `deliver-webhooks` runs,
    so a stalled cron shows up here as a growing pending count."""
    from sqlalchemy import func

    counts = dict(
        session.execute(
            select(WebhookDelivery.status, func.count(WebhookDelivery.id))
            .group_by(WebhookDelivery.status)
        ).all()
    )
    last = session.execute(select(func.max(WebhookDelivery.delivered_at))).scalar()
    oldest_pending = session.execute(
        select(func.min(WebhookDelivery.created_at)).where(
            WebhookDelivery.status == "pending"
        )
    ).scalar()
    active = session.execute(
        select(func.count(WebhookSubscription.id)).where(
            WebhookSubscription.is_active.is_(True)
        )
    ).scalar_one()
    return {
        "active_subscriptions": active,
        "pending": counts.get("pending", 0),
        "delivered": counts.get("delivered", 0),
        "failed": counts.get("failed", 0),
        "last_delivery_at": last,
        "oldest_pending_at": oldest_pending,
    }


def deliver_pending(session: Session, limit: int = 100) -> tuple[int, int]:
    """POST queued deliveries. Returns (delivered, failed)."""
    pending = session.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.status == "pending")
        .order_by(WebhookDelivery.id)  # per-subscription order preserved
        .limit(limit)
    ).scalars().all()
    delivered = failed = 0
    with httpx.Client(timeout=TIMEOUT) as client:
        for item in pending:
            sub = session.get(WebhookSubscription, item.subscription_id)
            if sub is None or not sub.is_active:
                item.status = "failed"
                item.last_error = "subscription inactive"
                failed += 1
                continue
            body = json.dumps(item.payload, sort_keys=True).encode()
            item.attempts += 1
            try:
                resp = client.post(
                    sub.url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-RIP-Signature": sign(sub.signing_secret, body),
                        "X-RIP-Event": item.event_type,
                        "X-RIP-Delivery-Id": str(item.id),
                    },
                )
                item.last_status_code = resp.status_code
                if 200 <= resp.status_code < 300:
                    item.status = "delivered"
                    item.delivered_at = _utcnow()
                    delivered += 1
                    continue
                item.last_error = f"HTTP {resp.status_code}"
            except Exception as exc:
                item.last_error = f"{type(exc).__name__}: {exc}"
            if item.attempts >= item.max_attempts:
                item.status = "failed"
                failed += 1
            else:
                # jittered backoff so a recovering endpoint isn't stampeded
                time.sleep(min(5.0, 0.5 * item.attempts + secrets.randbelow(500) / 1000))
        session.commit()
    return delivered, failed
