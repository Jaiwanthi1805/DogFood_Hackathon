"""
SQLAlchemy ORM models for the DOGFOOD 2026 portal.
All IDs are strings (fixture-compatible).
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Text, DateTime, Boolean, ForeignKey,
    Table, Integer, JSON, UniqueConstraint, Index
)
from sqlalchemy.orm import relationship
from database import Base


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Users ────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False)  # organizer | participant | judge
    is_seeded = Column(Boolean, default=False)   # from fixtures

    # Relations
    team_memberships = relationship("TeamMember", back_populates="user")
    projects = relationship("Project", back_populates="owner")
    tokens = relationship("AuthToken", back_populates="user")


class AuthToken(Base):
    """Simple bearer token auth. One per user for seed accounts."""
    __tablename__ = "auth_tokens"

    token = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=utcnow)

    user = relationship("User", back_populates="tokens")


# ── Events ───────────────────────────────────────────────────────────────────

class Event(Base):
    __tablename__ = "events"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(Text, default="")
    submissions_close = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    created_by = Column(String, ForeignKey("users.id"), nullable=True)
    is_seeded = Column(Boolean, default=False)

    tracks = relationship("Track", back_populates="event", cascade="all, delete-orphan")
    registrations = relationship("EventRegistration", back_populates="event")


class EventRegistration(Base):
    __tablename__ = "event_registrations"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    event_id = Column(String, ForeignKey("events.id"), nullable=False)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    registered_at = Column(DateTime, default=utcnow)

    event = relationship("Event", back_populates="registrations")
    user = relationship("User")


class EventVotingConfig(Base):
    """Community voting settings kept separate from legacy event rows."""
    __tablename__ = "event_voting_configs"

    event_id = Column(String, ForeignKey("events.id"), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    eligibility = Column(String, nullable=False, default="registered_participants")
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)
    hide_results = Column(Boolean, nullable=False, default=True)

    event = relationship("Event")


class CommunityVote(Base):
    __tablename__ = "community_votes"
    __table_args__ = (
        UniqueConstraint("event_id", "voter_id", name="uq_community_vote_event_voter"),
        Index("ix_community_votes_event_project", "event_id", "project_id"),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    event_id = Column(String, ForeignKey("events.id"), nullable=False)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False)
    voter_id = Column(String, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class ProjectComment(Base):
    __tablename__ = "project_comments"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, ForeignKey("projects.id"), nullable=False, index=True)
    author_id = Column(String, ForeignKey("users.id"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    author = relationship("User")


class VotingRateLimitHit(Base):
    __tablename__ = "voting_rate_limit_hits"
    __table_args__ = (
        Index("ix_voting_rate_limit_actor_time", "event_id", "voter_id", "created_at"),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    event_id = Column(String, ForeignKey("events.id"), nullable=False)
    voter_id = Column(String, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class VotingAuditRecord(Base):
    __tablename__ = "voting_audit_records"
    __table_args__ = (Index("ix_voting_audit_event_time", "event_id", "created_at"),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    event_id = Column(String, ForeignKey("events.id"), nullable=False)
    actor_id = Column(String, ForeignKey("users.id"), nullable=True)
    action = Column(String, nullable=False)
    project_id = Column(String, ForeignKey("projects.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class IssuedRecord(Base):
    __tablename__ = "issued_records"
    __table_args__ = (
        UniqueConstraint("event_id", "record_type", "subject_id", name="uq_issued_record_subject"),
        Index("ix_issued_records_event_type", "event_id", "record_type"),
    )

    id = Column(String, primary_key=True)
    event_id = Column(String, ForeignKey("events.id"), nullable=False)
    record_type = Column(String, nullable=False)
    subject_id = Column(String, ForeignKey("users.id"), nullable=False)
    payload_json = Column(JSON, nullable=False)
    signature = Column(Text, nullable=False)
    public_key = Column(Text, nullable=False)
    key_id = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"
    __table_args__ = (Index("ix_webhook_subscriptions_event", "event_id", "active"),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    event_id = Column(String, ForeignKey("events.id"), nullable=True)
    target_url = Column(String, nullable=False)
    event_types = Column(JSON, nullable=False)
    created_by = Column(String, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    active = Column(Boolean, nullable=False, default=True)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id = Column(String, primary_key=True)
    subscription_id = Column(String, ForeignKey("webhook_subscriptions.id"), nullable=False)
    event_id = Column(String, ForeignKey("events.id"), nullable=False)
    event_type = Column(String, nullable=False)
    payload_json = Column(JSON, nullable=False)
    status = Column(String, nullable=False)
    response_status = Column(Integer, nullable=True)
    attempts = Column(Integer, nullable=False, default=1)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    delivered_at = Column(DateTime, nullable=True)


# ── Tracks ───────────────────────────────────────────────────────────────────

class Track(Base):
    __tablename__ = "tracks"

    id = Column(String, primary_key=True)
    event_id = Column(String, ForeignKey("events.id"), nullable=False)
    name = Column(String, nullable=False)

    event = relationship("Event", back_populates="tracks")
    projects = relationship("Project", back_populates="track")


# ── Judges ───────────────────────────────────────────────────────────────────

class JudgeTrack(Base):
    """Maps a judge (User with role=judge) to tracks they evaluate."""
    __tablename__ = "judge_tracks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    judge_id = Column(String, nullable=False)   # fixture judge id e.g. jdg_01
    track_id = Column(String, ForeignKey("tracks.id"), nullable=False)
    user_id = Column(String, ForeignKey("users.id"), nullable=True)  # linked User row


# ── Teams ────────────────────────────────────────────────────────────────────

class Team(Base):
    __tablename__ = "teams"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    event_id = Column(String, ForeignKey("events.id"), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    is_seeded = Column(Boolean, default=False)

    members = relationship("TeamMember", back_populates="team", cascade="all, delete-orphan")
    projects = relationship("Project", back_populates="team")


class TeamMember(Base):
    __tablename__ = "team_members"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    team_id = Column(String, ForeignKey("teams.id"), nullable=False)
    user_id = Column(String, ForeignKey("users.id"), nullable=True)
    email = Column(String, nullable=False)   # from fixture: email-only members
    joined_at = Column(DateTime, default=utcnow)

    team = relationship("Team", back_populates="members")
    user = relationship("User", back_populates="team_memberships")


# ── Projects ─────────────────────────────────────────────────────────────────

class Project(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True)
    team_id = Column(String, ForeignKey("teams.id"), nullable=False)
    track_id = Column(String, ForeignKey("tracks.id"), nullable=True)
    event_id = Column(String, ForeignKey("events.id"), nullable=True)
    owner_id = Column(String, ForeignKey("users.id"), nullable=True)  # creating user
    title = Column(String, nullable=False)
    summary = Column(Text, default="")
    repo_url = Column(String, default="")
    status = Column(String, default="draft")  # draft | submitted
    submitted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    is_seeded = Column(Boolean, default=False)

    team = relationship("Team", back_populates="projects")
    track = relationship("Track", back_populates="projects")
    owner = relationship("User", back_populates="projects")
    scores = relationship("Score", back_populates="project", cascade="all, delete-orphan")


# ── Scores ───────────────────────────────────────────────────────────────────

class Score(Base):
    """
    Stores judge scores. Retained from fixtures for T2 use.
    criteria stored as JSON dict e.g. {"functionality": 4, "quality": 3}
    """
    __tablename__ = "scores"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    judge_fixture_id = Column(String, nullable=False)   # e.g. jdg_01
    judge_user_id = Column(String, ForeignKey("users.id"), nullable=True)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False)
    criteria = Column(JSON, nullable=False, default=dict)
    comment = Column(Text, default="")
    scored_at = Column(DateTime, default=utcnow)

    project = relationship("Project", back_populates="scores")
