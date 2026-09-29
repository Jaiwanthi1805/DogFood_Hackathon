"""T4 signed records and certificates."""
import hashlib
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth import require_organizer
from database import get_db
import models
from t4_signing import (
    SigningKeyError,
    load_or_create_private_key,
    public_key_info,
    sign_payload,
    verify_payload,
)
from webhooks import dispatch_webhook_event, validate_target_url

router = APIRouter(tags=["T4 records"])


class RecordVerificationInput(BaseModel):
    payload: Dict[str, Any]
    signature: str = Field(min_length=1, max_length=256)
    key_id: str = Field(min_length=1, max_length=64)
    public_key: str = Field(min_length=1, max_length=128)


class WebhookSubscriptionInput(BaseModel):
    event_id: str | None = None
    target_url: str = Field(min_length=1, max_length=2048)
    event_types: List[Literal[
        "event.created", "event.updated", "participant.registered", "track.created",
        "team.changed", "project.changed", "judge.score_submitted", "community.changed",
        "record.issued", "webhook.subscription_changed", "data.imported",
    ]]


class BulkImportRequest(BaseModel):
    target_event_id: str = Field(min_length=4, max_length=80)
    archive: Dict[str, Any]


PROTECTED_EVENT_IDS = {"evt_01", "evt_t3"}


def _date_text(value):
    return value.isoformat() + "Z" if value else None


def _parse_date(value, field_name):
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"{field_name} must be an ISO 8601 string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} must be an ISO 8601 string") from exc


def _archive_list(archive, field_name):
    value = archive.get(field_name)
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail=f"archive.{field_name} must be a list")
    if len(value) > 10000:
        raise HTTPException(status_code=413, detail=f"archive.{field_name} is too large")
    if not all(isinstance(item, dict) for item in value):
        raise HTTPException(status_code=422, detail=f"archive.{field_name} entries must be objects")
    return value


def _archive_id(value, field_name):
    if not isinstance(value, str) or not value or len(value) > 160:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a non-empty ID string")
    return value


def _collision_safe_id(db, model, source_id, target_event_id, prefix):
    if not db.query(model.id).filter_by(id=source_id).first():
        return source_id
    digest = hashlib.sha256(f"{target_event_id}\0{source_id}".encode("utf-8")).hexdigest()[:20]
    candidate = f"{prefix}_{digest}"
    if db.query(model.id).filter_by(id=candidate).first():
        raise HTTPException(status_code=409, detail=f"Unable to safely remap imported ID {source_id}")
    return candidate


def _resolve_user(db, user_id, email, field_name, required=False):
    user = None
    if user_id:
        user = db.query(models.User).filter_by(id=user_id).first()
    if user is None and email:
        user = db.query(models.User).filter(func.lower(models.User.email) == str(email).lower()).first()
    if user is None and required:
        raise HTTPException(status_code=422, detail=f"{field_name} references an unknown user")
    return user


def _signing_key():
    try:
        return load_or_create_private_key()
    except SigningKeyError as exc:
        raise HTTPException(status_code=503, detail="T4 signing key is unavailable") from exc


def _record_dict(record: models.IssuedRecord) -> dict:
    return {
        "id": record.id,
        "payload": record.payload_json,
        "signature": record.signature,
        "key_id": record.key_id,
        "public_key": record.public_key,
    }


def _issue_record(
    db: Session,
    event: models.Event,
    record_type: str,
    subject: models.User,
    data: dict,
) -> models.IssuedRecord:
    existing = db.query(models.IssuedRecord).filter_by(
        event_id=event.id,
        record_type=record_type,
        subject_id=subject.id,
    ).first()
    if existing:
        return existing

    record_id = "rec_" + hashlib.sha256(
        f"{event.id}\0{record_type}\0{subject.id}".encode("utf-8")
    ).hexdigest()[:32]
    payload = {
        "version": 1,
        "record_id": record_id,
        "record_type": record_type,
        "event_id": event.id,
        "event_name": event.name,
        "subject_id": subject.id,
        "issued_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "data": data,
    }
    private_key = _signing_key()
    signed = sign_payload(payload, private_key)
    record = models.IssuedRecord(
        id=record_id,
        event_id=event.id,
        record_type=record_type,
        subject_id=subject.id,
        payload_json=payload,
        signature=signed["signature"],
        public_key=signed["public_key"],
        key_id=signed["key_id"],
    )
    db.add(record)
    db.flush()
    return record


def _trusted_signature(payload: dict, signature: str, key_id: str, public_key: str) -> bool:
    try:
        current_public = public_key_info(_signing_key())
    except HTTPException:
        return False
    return (
        key_id == current_public["key_id"]
        and public_key == current_public["public_key"]
        and verify_payload(payload, signature, public_key)
    )


@router.get("/api/t4/signing/public-key", summary="Get the T4 verification key")
def get_t4_public_key():
    """Expose the Ed25519 public key and fingerprint, never the private key."""
    return {"algorithm": "Ed25519", **public_key_info(_signing_key())}


@router.post("/api/events/{event_id}/records/generate", summary="Issue participant certificates and judge records")
def generate_event_records(
    event_id: str,
    current_user: models.User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    """Idempotently issue public participant certificates and signed judge participation records."""
    event = db.query(models.Event).filter_by(id=event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    participants = db.query(models.User).join(models.EventRegistration).filter(
        models.EventRegistration.event_id == event_id,
        models.User.role == "participant",
    ).order_by(models.User.id).all()
    newly_issued = []
    for participant in participants:
        project_count = db.query(models.Project.id).join(
            models.TeamMember, models.TeamMember.team_id == models.Project.team_id
        ).filter(
            models.Project.event_id == event_id,
            models.Project.status == "submitted",
            models.TeamMember.user_id == participant.id,
        ).count()
        existing = db.query(models.IssuedRecord.id).filter_by(
            event_id=event_id,
            record_type="participant_certificate",
            subject_id=participant.id,
        ).first()
        record = _issue_record(
            db,
            event,
            "participant_certificate",
            participant,
            {"award": "participant", "submitted_project_count": project_count},
        )
        if not existing:
            newly_issued.append(record)

    judge_score_counts = db.query(
        models.Score.judge_user_id,
        func.count(models.Score.id),
    ).join(models.Project, models.Project.id == models.Score.project_id).filter(
        models.Project.event_id == event_id,
        models.Project.status == "submitted",
        models.Score.judge_user_id.isnot(None),
    ).group_by(models.Score.judge_user_id).all()
    for judge_id, score_count in judge_score_counts:
        judge = db.query(models.User).filter_by(id=judge_id, role="judge").first()
        if judge:
            existing = db.query(models.IssuedRecord.id).filter_by(
                event_id=event_id,
                record_type="judge_participation",
                subject_id=judge.id,
            ).first()
            record = _issue_record(
                db,
                event,
                "judge_participation",
                judge,
                {"scored_project_count": score_count},
            )
            if not existing:
                newly_issued.append(record)

    db.commit()
    for record in newly_issued:
        dispatch_webhook_event(
            db,
            event_id,
            "record.issued",
            record.record_type,
            record.id,
            {"record_id": record.id, "record_type": record.record_type},
        )
    records = db.query(models.IssuedRecord).filter_by(event_id=event_id).order_by(
        models.IssuedRecord.record_type,
        models.IssuedRecord.subject_id,
    ).all()
    return {"event_id": event_id, "records": [_record_dict(record) for record in records]}


@router.get("/api/t4/records/{record_id}", summary="Retrieve a public issued record")
def get_t4_record(record_id: str, db: Session = Depends(get_db)):
    record = db.query(models.IssuedRecord).filter_by(id=record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return _record_dict(record)


@router.get("/api/t4/records/{record_id}/verify", summary="Verify a stored issued record")
def verify_t4_record(record_id: str, db: Session = Depends(get_db)):
    record = db.query(models.IssuedRecord).filter_by(id=record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return {
        "record_id": record.id,
        "key_id": record.key_id,
        "valid": _trusted_signature(
            record.payload_json, record.signature, record.key_id, record.public_key
        ),
    }


@router.post("/api/t4/records/verify", summary="Verify a signed record payload")
def verify_t4_payload(body: RecordVerificationInput):
    """Verify canonical payload bytes against this portal's current public signing key."""
    return {
        "key_id": body.key_id,
        "valid": _trusted_signature(body.payload, body.signature, body.key_id, body.public_key),
    }


@router.get("/api/events/{event_id}/webhooks", summary="List event webhook subscriptions")
def list_webhook_subscriptions(
    event_id: str,
    current_user: models.User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    if not db.query(models.Event.id).filter_by(id=event_id).first():
        raise HTTPException(status_code=404, detail="Event not found")
    subscriptions = db.query(models.WebhookSubscription).filter_by(event_id=event_id).order_by(
        models.WebhookSubscription.created_at
    ).all()
    return [{
        "id": item.id,
        "event_id": item.event_id,
        "target_url": item.target_url,
        "event_types": item.event_types,
        "active": item.active,
    } for item in subscriptions]


@router.get("/api/webhooks", summary="List organizer webhook subscriptions")
def list_global_webhook_subscriptions(
    current_user: models.User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    subscriptions = db.query(models.WebhookSubscription).order_by(
        models.WebhookSubscription.created_at
    ).all()
    return [{
        "id": item.id,
        "event_id": item.event_id,
        "target_url": item.target_url,
        "event_types": item.event_types,
        "active": item.active,
    } for item in subscriptions]


@router.post("/api/webhooks", status_code=201, summary="Create global or event-scoped webhook subscription")
def create_global_webhook_subscription(
    body: WebhookSubscriptionInput,
    current_user: models.User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    if body.event_id and not db.query(models.Event.id).filter_by(id=body.event_id).first():
        raise HTTPException(status_code=404, detail="Event not found")
    try:
        validate_target_url(body.target_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    event_types = sorted(set(body.event_types))
    if not event_types:
        raise HTTPException(status_code=422, detail="Select at least one webhook event type")
    subscription = models.WebhookSubscription(
        event_id=body.event_id,
        target_url=body.target_url,
        event_types=event_types,
        created_by=current_user.id,
        active=True,
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    if subscription.event_id:
        dispatch_webhook_event(
            db,
            subscription.event_id,
            "webhook.subscription_changed",
            "created",
            subscription.id,
            {"subscription_id": subscription.id},
        )
    return {
        "id": subscription.id,
        "event_id": subscription.event_id,
        "target_url": subscription.target_url,
        "event_types": subscription.event_types,
        "active": subscription.active,
    }


@router.delete("/api/webhooks/{subscription_id}", summary="Disable an organizer webhook subscription")
def disable_global_webhook_subscription(
    subscription_id: str,
    current_user: models.User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    subscription = db.query(models.WebhookSubscription).filter_by(id=subscription_id).first()
    if not subscription:
        raise HTTPException(status_code=404, detail="Webhook subscription not found")
    subscription.active = False
    db.commit()
    if subscription.event_id:
        dispatch_webhook_event(
            db,
            subscription.event_id,
            "webhook.subscription_changed",
            "disabled",
            subscription.id,
            {"subscription_id": subscription.id},
        )
    return {"id": subscription.id, "active": False}


@router.post("/api/events/{event_id}/webhooks", status_code=201, summary="Create event webhook subscription")
def create_webhook_subscription(
    event_id: str,
    body: WebhookSubscriptionInput,
    current_user: models.User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    if not db.query(models.Event.id).filter_by(id=event_id).first():
        raise HTTPException(status_code=404, detail="Event not found")
    try:
        validate_target_url(body.target_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    event_types = sorted(set(body.event_types))
    if not event_types:
        raise HTTPException(status_code=422, detail="Select at least one webhook event type")
    subscription = models.WebhookSubscription(
        event_id=event_id,
        target_url=body.target_url,
        event_types=event_types,
        created_by=current_user.id,
        active=True,
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    dispatch_webhook_event(
        db,
        event_id,
        "webhook.subscription_changed",
        "created",
        subscription.id,
        {"subscription_id": subscription.id},
    )
    return {
        "id": subscription.id,
        "event_id": event_id,
        "target_url": subscription.target_url,
        "event_types": subscription.event_types,
        "active": subscription.active,
    }


@router.delete("/api/events/{event_id}/webhooks/{subscription_id}", summary="Disable event webhook subscription")
def disable_webhook_subscription(
    event_id: str,
    subscription_id: str,
    current_user: models.User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    subscription = db.query(models.WebhookSubscription).filter_by(
        event_id=event_id, id=subscription_id
    ).first()
    if not subscription:
        raise HTTPException(status_code=404, detail="Webhook subscription not found")
    subscription.active = False
    db.commit()
    dispatch_webhook_event(
        db,
        event_id,
        "webhook.subscription_changed",
        "disabled",
        subscription.id,
        {"subscription_id": subscription.id},
    )
    return {"id": subscription.id, "active": False}


@router.get("/api/events/{event_id}/webhooks/deliveries", summary="List event webhook delivery results")
def list_webhook_deliveries(
    event_id: str,
    current_user: models.User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    deliveries = db.query(models.WebhookDelivery).filter_by(event_id=event_id).order_by(
        models.WebhookDelivery.created_at.desc()
    ).limit(100).all()
    return [{
        "delivery_id": item.id,
        "event_type": item.event_type,
        "status": item.status,
        "response_status": item.response_status,
        "attempts": item.attempts,
        "error": item.error,
        "timestamp": item.payload_json.get("timestamp"),
    } for item in deliveries]


@router.get("/api/events/{event_id}/bulk-export", summary="Export an event archive, schema version 1")
def export_event_archive(
    event_id: str,
    current_user: models.User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    """Export event setup and judging data as versioned JSON; secrets/webhook destinations are excluded."""
    event = db.query(models.Event).filter_by(id=event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    tracks = db.query(models.Track).filter_by(event_id=event_id).order_by(models.Track.id).all()
    teams = db.query(models.Team).filter_by(event_id=event_id).order_by(models.Team.id).all()
    projects = db.query(models.Project).filter_by(event_id=event_id).order_by(models.Project.id).all()
    registrations = db.query(models.EventRegistration).filter_by(event_id=event_id).order_by(
        models.EventRegistration.user_id
    ).all()
    track_ids = [track.id for track in tracks]
    assignments = db.query(models.JudgeTrack).filter(
        models.JudgeTrack.track_id.in_(track_ids)
    ).order_by(models.JudgeTrack.judge_id, models.JudgeTrack.track_id).all() if track_ids else []
    project_ids = [project.id for project in projects]
    scores = db.query(models.Score).filter(
        models.Score.project_id.in_(project_ids)
    ).order_by(models.Score.id).all() if project_ids else []
    voting_config = db.query(models.EventVotingConfig).filter_by(event_id=event_id).first()

    archive = {
        "schema_version": 1,
        "event": {
            "id": event.id,
            "name": event.name,
            "description": event.description or "",
            "submissions_close": _date_text(event.submissions_close),
        },
        "tracks": [{"id": track.id, "name": track.name} for track in tracks],
        "registrations": [{
            "user_id": registration.user_id,
            "email": registration.user.email,
        } for registration in registrations],
        "teams": [{
            "id": team.id,
            "name": team.name,
            "members": [{"user_id": member.user_id, "email": member.email}
                        for member in sorted(team.members, key=lambda member: member.email)],
        } for team in teams],
        "projects": [{
            "id": project.id,
            "team_id": project.team_id,
            "track_id": project.track_id,
            "owner_id": project.owner_id,
            "title": project.title,
            "summary": project.summary or "",
            "repo_url": project.repo_url or "",
            "status": project.status,
            "submitted_at": _date_text(project.submitted_at),
        } for project in projects],
        "judge_assignments": [{
            "judge_id": assignment.judge_id,
            "track_id": assignment.track_id,
            "user_id": assignment.user_id,
        } for assignment in assignments],
        "scores": [{
            "id": score.id,
            "judge_fixture_id": score.judge_fixture_id,
            "judge_user_id": score.judge_user_id,
            "project_id": score.project_id,
            "criteria": score.criteria,
            "comment": score.comment or "",
            "scored_at": _date_text(score.scored_at),
        } for score in scores],
        "voting_config": {
            "enabled": voting_config.enabled,
            "eligibility": voting_config.eligibility,
            "starts_at": _date_text(voting_config.starts_at),
            "ends_at": _date_text(voting_config.ends_at),
            "hide_results": voting_config.hide_results,
        } if voting_config else None,
    }
    return archive


@router.post("/api/events/bulk-import", status_code=201, summary="Import an event archive, schema version 1")
def import_event_archive(
    body: BulkImportRequest,
    current_user: models.User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    """Import into a new event; protected fixture IDs are never overwritten."""
    target_event_id = body.target_event_id
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,79}", target_event_id):
        raise HTTPException(status_code=422, detail="target_event_id contains unsupported characters")
    if target_event_id in PROTECTED_EVENT_IDS:
        raise HTTPException(status_code=409, detail="Protected event IDs cannot be import targets")
    if db.query(models.Event.id).filter_by(id=target_event_id).first():
        raise HTTPException(status_code=409, detail="Target event already exists")

    archive = body.archive
    if archive.get("schema_version") != 1:
        raise HTTPException(status_code=422, detail="Unsupported archive schema_version")
    event_data = archive.get("event")
    if not isinstance(event_data, dict):
        raise HTTPException(status_code=422, detail="archive.event must be an object")
    source_event_id = _archive_id(event_data.get("id"), "archive.event.id")
    if source_event_id in PROTECTED_EVENT_IDS:
        raise HTTPException(status_code=409, detail="Protected source event archives cannot be imported")
    event_name = event_data.get("name")
    if not isinstance(event_name, str) or not event_name.strip():
        raise HTTPException(status_code=422, detail="archive.event.name is required")
    submissions_close = _parse_date(event_data.get("submissions_close"), "archive.event.submissions_close")

    tracks_data = _archive_list(archive, "tracks")
    registrations_data = _archive_list(archive, "registrations")
    teams_data = _archive_list(archive, "teams")
    projects_data = _archive_list(archive, "projects")
    assignments_data = _archive_list(archive, "judge_assignments")
    scores_data = _archive_list(archive, "scores")
    track_map = {}
    team_map = {}
    project_map = {}
    for track in tracks_data:
        old_id = _archive_id(track.get("id"), "archive.tracks[].id")
        if old_id in track_map:
            raise HTTPException(status_code=422, detail="archive contains duplicate track IDs")
        track_map[old_id] = _collision_safe_id(db, models.Track, old_id, target_event_id, "t4track")
    for team in teams_data:
        old_id = _archive_id(team.get("id"), "archive.teams[].id")
        if old_id in team_map:
            raise HTTPException(status_code=422, detail="archive contains duplicate team IDs")
        team_map[old_id] = _collision_safe_id(db, models.Team, old_id, target_event_id, "t4team")
        if not isinstance(team.get("name"), str) or not team["name"].strip():
            raise HTTPException(status_code=422, detail="archive team name is required")
        if not isinstance(team.get("members", []), list):
            raise HTTPException(status_code=422, detail="archive team members must be a list")
    for project in projects_data:
        old_id = _archive_id(project.get("id"), "archive.projects[].id")
        if old_id in project_map:
            raise HTTPException(status_code=422, detail="archive contains duplicate project IDs")
        project_map[old_id] = _collision_safe_id(db, models.Project, old_id, target_event_id, "t4project")
        if project.get("team_id") not in team_map:
            raise HTTPException(status_code=422, detail="project references a missing team")
        if project.get("track_id") is not None and project.get("track_id") not in track_map:
            raise HTTPException(status_code=422, detail="project references a missing track")
        if project.get("status") not in {"draft", "submitted"}:
            raise HTTPException(status_code=422, detail="project status must be draft or submitted")
        if not isinstance(project.get("title"), str) or not project["title"].strip():
            raise HTTPException(status_code=422, detail="project title is required")

    for track in tracks_data:
        if not isinstance(track.get("name"), str) or not track["name"].strip():
            raise HTTPException(status_code=422, detail="archive track name is required")
    for registration in registrations_data:
        _resolve_user(
            db, registration.get("user_id"), registration.get("email"),
            "registration", required=True,
        )
    voting_data = archive.get("voting_config")
    if voting_data is not None:
        if not isinstance(voting_data, dict):
            raise HTTPException(status_code=422, detail="voting_config must be an object or null")
        if voting_data.get("eligibility") not in {"registered_participants", "all_participants"}:
            raise HTTPException(status_code=422, detail="voting_config eligibility is invalid")
        if not all(isinstance(voting_data.get(flag), bool) for flag in ("enabled", "hide_results")):
            raise HTTPException(status_code=422, detail="voting_config flags must be booleans")
        voting_starts = _parse_date(voting_data.get("starts_at"), "voting_config.starts_at")
        voting_ends = _parse_date(voting_data.get("ends_at"), "voting_config.ends_at")
        if voting_starts >= voting_ends:
            raise HTTPException(status_code=422, detail="voting_config end must be after start")

    event = models.Event(
        id=target_event_id,
        name=event_name.strip(),
        description=event_data.get("description") or "",
        submissions_close=submissions_close,
        created_by=current_user.id,
        is_seeded=False,
    )
    db.add(event)
    db.flush()
    for track in tracks_data:
        db.add(models.Track(id=track_map[track["id"]], event_id=target_event_id, name=track["name"].strip()))

    team_members = {}
    for team in teams_data:
        target_team_id = team_map[team["id"]]
        db.add(models.Team(
            id=target_team_id,
            name=team["name"].strip(),
            event_id=target_event_id,
            is_seeded=False,
        ))
        for member in team.get("members", []):
            if not isinstance(member, dict):
                raise HTTPException(status_code=422, detail="team members must be objects")
            user = _resolve_user(db, member.get("user_id"), member.get("email"), "team member")
            member_email = member.get("email") or (user.email if user else None)
            if not isinstance(member_email, str) or not member_email:
                raise HTTPException(status_code=422, detail="team member email is required")
            db.add(models.TeamMember(
                team_id=target_team_id,
                user_id=user.id if user else None,
                email=member_email,
            ))
            if user:
                team_members[(team["id"], user.id)] = True

    registration_users = set()
    for registration in registrations_data:
        user = _resolve_user(db, registration.get("user_id"), registration.get("email"), "registration", required=True)
        if user.id not in registration_users:
            db.add(models.EventRegistration(event_id=target_event_id, user_id=user.id))
            registration_users.add(user.id)

    for project in projects_data:
        owner = _resolve_user(db, project.get("owner_id"), None, "project owner")
        db.add(models.Project(
            id=project_map[project["id"]],
            team_id=team_map[project["team_id"]],
            track_id=track_map.get(project.get("track_id")),
            event_id=target_event_id,
            owner_id=owner.id if owner else None,
            title=project["title"].strip(),
            summary=project.get("summary") or "",
            repo_url=project.get("repo_url") or "",
            status=project["status"],
            submitted_at=_parse_date(project["submitted_at"], "project.submitted_at")
                if project.get("submitted_at") else None,
            is_seeded=False,
        ))

    for assignment in assignments_data:
        if assignment.get("track_id") not in track_map:
            raise HTTPException(status_code=422, detail="judge assignment references a missing track")
        assigned_user = _resolve_user(db, assignment.get("user_id"), None, "judge assignment")
        db.add(models.JudgeTrack(
            judge_id=_archive_id(assignment.get("judge_id"), "judge assignment judge_id"),
            track_id=track_map[assignment["track_id"]],
            user_id=assigned_user.id if assigned_user else None,
        ))

    for score in scores_data:
        if score.get("project_id") not in project_map:
            raise HTTPException(status_code=422, detail="score references a missing project")
        if not isinstance(score.get("criteria"), dict):
            raise HTTPException(status_code=422, detail="score criteria must be an object")
        judge = _resolve_user(db, score.get("judge_user_id"), None, "score judge")
        score_id = _archive_id(score.get("id"), "score id")
        score_id = _collision_safe_id(db, models.Score, score_id, target_event_id, "t4score")
        scored_at = _parse_date(score["scored_at"], "score.scored_at") if score.get("scored_at") else None
        db.add(models.Score(
            id=score_id,
            judge_fixture_id=_archive_id(score.get("judge_fixture_id"), "score judge_fixture_id"),
            judge_user_id=judge.id if judge else None,
            project_id=project_map[score["project_id"]],
            criteria=score["criteria"],
            comment=score.get("comment") or "",
            scored_at=scored_at,
        ))

    if voting_data:
        db.add(models.EventVotingConfig(
            event_id=target_event_id,
            enabled=voting_data["enabled"],
            eligibility=voting_data["eligibility"],
            starts_at=voting_starts,
            ends_at=voting_ends,
            hide_results=voting_data["hide_results"],
        ))

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail="Archive could not be imported consistently") from exc
    dispatch_webhook_event(
        db,
        target_event_id,
        "data.imported",
        "imported",
        target_event_id,
        {"source_event_id": source_event_id},
    )
    return {
        "event_id": target_event_id,
        "schema_version": 1,
        "tracks_imported": len(tracks_data),
        "registrations_imported": len(registration_users),
        "teams_imported": len(teams_data),
        "projects_imported": len(projects_data),
        "scores_imported": len(scores_data),
    }