"""Small synchronous webhook dispatcher; deliveries never fail the source action."""
import hashlib
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import models

WEBHOOK_EVENT_TYPES = {
    "event.created",
    "event.updated",
    "participant.registered",
    "track.created",
    "team.changed",
    "project.changed",
    "judge.score_submitted",
    "community.changed",
    "record.issued",
    "webhook.subscription_changed",
    "data.imported",
}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def validate_target_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("Webhook URL must be an HTTP(S) URL without credentials or a fragment")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError) as exc:
        raise ValueError("Webhook host could not be resolved") from exc
    if not addresses:
        raise ValueError("Webhook host did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not (ip.is_global or ip.is_loopback):
            raise ValueError("Webhook destinations must be public or loopback addresses")


def _delivery_id(
    subscription_id: str,
    event_id: str,
    event_type: str,
    action: str,
    source_key: str,
    payload: dict,
    emitted_at: str,
) -> str:
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    raw = "\0".join((subscription_id, event_id, event_type, action, source_key, emitted_at))
    raw = raw.encode("utf-8") + b"\0" + payload_bytes
    return "whd_" + hashlib.sha256(raw).hexdigest()[:32]


def _dispatch_webhook_event(
    db: Session,
    event_id: str,
    event_type: str,
    action: str,
    source_key: str,
    payload: dict,
) -> None:
    if event_type not in WEBHOOK_EVENT_TYPES:
        return
    emitted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    subscriptions = db.query(models.WebhookSubscription).filter(
        models.WebhookSubscription.active.is_(True),
        (models.WebhookSubscription.event_id == event_id)
        | (models.WebhookSubscription.event_id.is_(None)),
    ).all()
    for subscription in subscriptions:
        if event_type not in subscription.event_types:
            continue
        delivery_id = _delivery_id(
            subscription.id, event_id, event_type, action, source_key, payload, emitted_at
        )
        if db.query(models.WebhookDelivery.id).filter_by(id=delivery_id).first():
            continue
        envelope = {
            "delivery_id": delivery_id,
            "event_type": event_type,
            "event_id": event_id,
            "timestamp": emitted_at,
            "payload": {"action": action, **payload},
        }
        status = "failed"
        response_status = None
        error = None
        delivered_at = None
        try:
            validate_target_url(subscription.target_url)
            request = urllib.request.Request(
                subscription.target_url,
                data=json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "DOGFOOD-T4-Webhook/1"},
                method="POST",
            )
            opener = urllib.request.build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=1.5) as response:
                response_status = response.status
            status = "delivered" if 200 <= response_status < 300 else "failed"
            if status == "delivered":
                delivered_at = datetime.now(timezone.utc).replace(tzinfo=None)
        except urllib.error.HTTPError as exc:
            response_status = exc.code
            error = f"HTTP {exc.code}"
        except Exception as exc:
            error = str(exc)[:500]

        db.add(models.WebhookDelivery(
            id=delivery_id,
            subscription_id=subscription.id,
            event_id=event_id,
            event_type=event_type,
            payload_json=envelope,
            status=status,
            response_status=response_status,
            attempts=1,
            error=error,
            delivered_at=delivered_at,
        ))
    try:
        db.commit()
    except Exception:
        db.rollback()


def dispatch_webhook_event(
    db: Session,
    event_id: str,
    event_type: str,
    action: str,
    source_key: str,
    payload: dict,
) -> None:
    try:
        _dispatch_webhook_event(db, event_id, event_type, action, source_key, payload)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass