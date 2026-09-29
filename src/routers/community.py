"""Community voting and comments for T3 events."""
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import get_current_user, require_organizer, require_user
from database import get_db
from webhooks import dispatch_webhook_event
import models

router = APIRouter(tags=["community"])

VOTE_LIMIT = 5
VOTE_WINDOW = timedelta(minutes=1)


class VotingConfigInput(BaseModel):
    enabled: bool = True
    eligibility: Literal["registered_participants", "all_participants"] = "registered_participants"
    starts_at: datetime
    ends_at: datetime
    hide_results: bool = True


class VoteInput(BaseModel):
    project_id: str


class CommentInput(BaseModel):
    body: str = Field(min_length=1, max_length=1000)


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _iso_utc(value: datetime) -> str:
    return value.isoformat() + "Z"


def _get_event(event_id: str, db: Session) -> models.Event:
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


def _get_config(event_id: str, db: Session) -> models.EventVotingConfig:
    config = db.query(models.EventVotingConfig).filter_by(event_id=event_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Community voting is not configured")
    return config


def _is_active(config: models.EventVotingConfig, now: Optional[datetime] = None) -> bool:
    now = now or datetime.utcnow()
    return config.enabled and config.starts_at <= now < config.ends_at


def _require_eligible(
    event_id: str,
    config: models.EventVotingConfig,
    user: models.User,
    db: Session,
    audit_rejection: bool = False,
):
    if user.role != "participant":
        if audit_rejection:
            _record_audit(db, event_id, user.id, "vote_rejected_ineligible")
            db.commit()
        raise HTTPException(status_code=403, detail="Only participants may use community voting")
    if config.eligibility == "registered_participants":
        registered = db.query(models.EventRegistration.id).filter_by(
            event_id=event_id, user_id=user.id
        ).first()
        if not registered:
            if audit_rejection:
                _record_audit(db, event_id, user.id, "vote_rejected_ineligible")
                db.commit()
            raise HTTPException(status_code=403, detail="Register for this event to participate in voting")


def _record_audit(
    db: Session,
    event_id: str,
    actor_id: Optional[str],
    action: str,
    project_id: Optional[str] = None,
):
    db.add(models.VotingAuditRecord(
        id=str(uuid.uuid4()),
        event_id=event_id,
        actor_id=actor_id,
        action=action,
        project_id=project_id,
    ))


@router.put("/api/events/{event_id}/voting")
def configure_voting(
    event_id: str,
    body: VotingConfigInput,
    current_user: models.User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    _get_event(event_id, db)
    starts_at = _utc_naive(body.starts_at)
    ends_at = _utc_naive(body.ends_at)
    if starts_at >= ends_at:
        raise HTTPException(status_code=422, detail="Voting end must be after voting start")

    config = db.query(models.EventVotingConfig).filter_by(event_id=event_id).first()
    if config is None:
        config = models.EventVotingConfig(event_id=event_id)
        db.add(config)
    config.enabled = body.enabled
    config.eligibility = body.eligibility
    config.starts_at = starts_at
    config.ends_at = ends_at
    config.hide_results = body.hide_results
    _record_audit(db, event_id, current_user.id, "voting_configured")
    db.commit()
    dispatch_webhook_event(
        db,
        event_id,
        "community.changed",
        "voting_configured",
        event_id,
        {"enabled": config.enabled, "hide_results": config.hide_results},
    )
    return {
        "event_id": event_id,
        "enabled": config.enabled,
        "eligibility": config.eligibility,
        "starts_at": _iso_utc(config.starts_at),
        "ends_at": _iso_utc(config.ends_at),
        "hide_results": config.hide_results,
    }


@router.get("/api/events/{event_id}/voting")
def get_voting_config(event_id: str, db: Session = Depends(get_db)):
    _get_event(event_id, db)
    config = _get_config(event_id, db)
    return {
        "event_id": event_id,
        "enabled": config.enabled,
        "eligibility": config.eligibility,
        "starts_at": _iso_utc(config.starts_at),
        "ends_at": _iso_utc(config.ends_at),
        "hide_results": config.hide_results,
        "active": _is_active(config),
    }


@router.get("/api/events/{event_id}/voting/projects")
def voting_projects(
    event_id: str,
    current_user: models.User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _get_event(event_id, db)
    config = _get_config(event_id, db)
    _require_eligible(event_id, config, current_user, db)
    if not _is_active(config):
        raise HTTPException(status_code=409, detail="Community voting is not active")
    projects = db.query(models.Project).filter_by(event_id=event_id, status="submitted").all()
    secrets.SystemRandom().shuffle(projects)
    has_voted = db.query(models.CommunityVote.id).filter_by(
        event_id=event_id, voter_id=current_user.id
    ).first() is not None
    return [{
        "id": project.id,
        "title": project.title,
        "summary": project.summary,
        "track_id": project.track_id,
        "team_id": project.team_id,
        "has_voted": has_voted,
    } for project in projects]


@router.post("/api/events/{event_id}/voting/votes", status_code=201)
def cast_vote(
    event_id: str,
    body: VoteInput,
    current_user: models.User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _get_event(event_id, db)
    config = _get_config(event_id, db)
    _require_eligible(event_id, config, current_user, db, audit_rejection=True)
    if not _is_active(config):
        _record_audit(db, event_id, current_user.id, "vote_rejected_window")
        db.commit()
        raise HTTPException(status_code=409, detail="Community voting is not active")

    now = datetime.utcnow()
    db.query(models.VotingRateLimitHit).filter(
        models.VotingRateLimitHit.event_id == event_id,
        models.VotingRateLimitHit.voter_id == current_user.id,
        models.VotingRateLimitHit.created_at < now - timedelta(days=1),
    ).delete(synchronize_session=False)
    recent_hits = db.query(models.VotingRateLimitHit).filter(
        models.VotingRateLimitHit.event_id == event_id,
        models.VotingRateLimitHit.voter_id == current_user.id,
        models.VotingRateLimitHit.created_at >= now - VOTE_WINDOW,
    ).count()
    if recent_hits >= VOTE_LIMIT:
        _record_audit(db, event_id, current_user.id, "vote_rate_limited")
        db.commit()
        raise HTTPException(status_code=429, detail="Voting rate limit exceeded")
    db.add(models.VotingRateLimitHit(event_id=event_id, voter_id=current_user.id, created_at=now))
    db.commit()

    project = db.query(models.Project).filter_by(
        id=body.project_id, event_id=event_id, status="submitted"
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Submitted project not found in this event")

    existing = db.query(models.CommunityVote).filter_by(
        event_id=event_id, voter_id=current_user.id
    ).first()
    if existing:
        _record_audit(db, event_id, current_user.id, "duplicate_vote_rejected", body.project_id)
        db.commit()
        raise HTTPException(status_code=409, detail="A vote has already been recorded for this event")

    db.add(models.CommunityVote(
        id=str(uuid.uuid4()),
        event_id=event_id,
        project_id=project.id,
        voter_id=current_user.id,
        created_at=now,
    ))
    _record_audit(db, event_id, current_user.id, "vote_cast", project.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        _record_audit(db, event_id, current_user.id, "duplicate_vote_rejected", project.id)
        db.commit()
        raise HTTPException(status_code=409, detail="A vote has already been recorded for this event")
    dispatch_webhook_event(
        db,
        event_id,
        "community.changed",
        "vote_cast",
        f"{current_user.id}:{project.id}",
        {"project_id": project.id},
    )
    return {"message": "Vote recorded", "project_id": project.id}


@router.get("/api/events/{event_id}/voting/results")
def voting_results(
    event_id: str,
    current_user: Optional[models.User] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_event(event_id, db)
    config = _get_config(event_id, db)
    active = _is_active(config)
    if active and config.hide_results and (current_user is None or current_user.role != "organizer"):
        raise HTTPException(status_code=403, detail="Voting results are hidden while voting is active")

    totals = dict(db.query(
        models.CommunityVote.project_id, func.count(models.CommunityVote.id)
    ).filter_by(event_id=event_id).group_by(models.CommunityVote.project_id).all())
    projects = db.query(models.Project).filter_by(event_id=event_id, status="submitted").all()
    return {
        "event_id": event_id,
        "results": [{"project_id": project.id, "title": project.title, "votes": totals.get(project.id, 0)}
                    for project in projects],
    }


@router.get("/api/events/{event_id}/voting/audit")
def voting_audit(
    event_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    current_user: models.User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    _get_event(event_id, db)
    records = db.query(models.VotingAuditRecord).filter_by(event_id=event_id).order_by(
        models.VotingAuditRecord.created_at.desc()
    ).limit(limit).all()
    return [{
        "action": record.action,
        "actor_id": record.actor_id,
        "project_id": record.project_id,
        "created_at": _iso_utc(record.created_at),
    } for record in records]


@router.get("/api/projects/{project_id}/comments")
def list_project_comments(project_id: str, db: Session = Depends(get_db)):
    project = db.query(models.Project).filter_by(id=project_id, status="submitted").first()
    if not project:
        raise HTTPException(status_code=404, detail="Submitted project not found")
    comments = db.query(models.ProjectComment).filter_by(project_id=project_id).order_by(
        models.ProjectComment.created_at.asc()
    ).all()
    return [{
        "id": comment.id,
        "author": comment.author.name,
        "body": comment.body,
        "created_at": _iso_utc(comment.created_at),
    } for comment in comments]


@router.post("/api/projects/{project_id}/comments", status_code=201)
def add_project_comment(
    project_id: str,
    body: CommentInput,
    current_user: models.User = Depends(require_user),
    db: Session = Depends(get_db),
):
    project = db.query(models.Project).filter_by(id=project_id, status="submitted").first()
    if not project:
        raise HTTPException(status_code=404, detail="Submitted project not found")
    if current_user.role != "participant":
        if project.event_id:
            _record_audit(db, project.event_id, current_user.id, "comment_rejected_unauthorized", project_id)
            db.commit()
        raise HTTPException(status_code=403, detail="Only participants may comment on projects")
    clean_body = body.body.strip()
    if not clean_body:
        if project.event_id:
            _record_audit(db, project.event_id, current_user.id, "comment_rejected_validation", project_id)
            db.commit()
        raise HTTPException(status_code=422, detail="Comment cannot be blank")
    comment = models.ProjectComment(
        id=str(uuid.uuid4()),
        project_id=project_id,
        author_id=current_user.id,
        body=clean_body,
    )
    db.add(comment)
    if project.event_id:
        _record_audit(db, project.event_id, current_user.id, "comment_posted", project_id)
    db.commit()
    db.refresh(comment)
    if project.event_id:
        dispatch_webhook_event(
            db,
            project.event_id,
            "community.changed",
            "comment_posted",
            comment.id,
            {"project_id": project_id, "comment_id": comment.id},
        )
    return {
        "id": comment.id,
        "author": current_user.name,
        "body": comment.body,
        "created_at": _iso_utc(comment.created_at),
    }
