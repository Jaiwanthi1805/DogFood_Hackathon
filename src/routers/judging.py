"""
API router: Judging (scores, assigned projects, judging progress, and CSV export)
T2 implementation for DOGFOOD 2026.
"""
import io
import csv
import uuid
from datetime import datetime
from typing import Optional, Set
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db
import models
from auth import require_user, require_organizer
from webhooks import dispatch_webhook_event

router = APIRouter(tags=["judging"])


class ScoreSubmit(BaseModel):
    project_id: str
    criteria: dict
    comment: Optional[str] = ""


def get_user_judge_identifiers(user: models.User, db: Session) -> Set[str]:
    """Return all known identifier strings that correspond to this user as a judge."""
    ids: Set[str] = {user.id, user.id.lower(), user.email, user.email.lower()}
    prefix = user.email.split("@")[0].lower()
    ids.add(prefix)

    # From JudgeTrack table
    jts = db.query(models.JudgeTrack).filter(models.JudgeTrack.user_id == user.id).all()
    for jt in jts:
        ids.add(jt.judge_id)
        ids.add(jt.judge_id.lower())

    # Deterministic mapping for seed accounts
    if user.id == "seed_jdg_a" or "judge_a" in prefix:
        ids.update({"judge_a", "jdg_01", "seed_jdg_a", "judge_a@dogfood.local"})
    elif user.id == "seed_jdg_b" or "judge_b" in prefix:
        ids.update({"judge_b", "jdg_02", "seed_jdg_b", "judge_b@dogfood.local"})

    return ids


def resolve_judge_identifiers(judge_param: str, db: Session) -> Set[str]:
    """Resolve a judge query string into all matching identifier strings."""
    key = judge_param.strip()
    key_lower = key.lower()
    ids: Set[str] = {key, key_lower}

    if key_lower in ("judge_a", "seed_jdg_a", "jdg_01"):
        ids.update({"judge_a", "seed_jdg_a", "jdg_01", "judge_a@dogfood.local"})
    elif key_lower in ("judge_b", "seed_jdg_b", "jdg_02"):
        ids.update({"judge_b", "seed_jdg_b", "jdg_02", "judge_b@dogfood.local"})
    else:
        # Check JudgeTrack
        jts = db.query(models.JudgeTrack).filter(
            (models.JudgeTrack.judge_id == key) |
            (models.JudgeTrack.user_id == key)
        ).all()
        for jt in jts:
            ids.add(jt.judge_id)
            ids.add(jt.judge_id.lower())
            if jt.user_id:
                ids.add(jt.user_id)
                ids.add(jt.user_id.lower())

        # Check User table
        u = db.query(models.User).filter(
            (models.User.id == key) |
            (models.User.email == key)
        ).first()
        if u:
            ids.add(u.id)
            ids.add(u.id.lower())
            ids.add(u.email)
            ids.add(u.email.lower())
            ids.add(u.email.split("@")[0].lower())
            for jt in db.query(models.JudgeTrack).filter(models.JudgeTrack.user_id == u.id).all():
                ids.add(jt.judge_id)
                ids.add(jt.judge_id.lower())

    return ids


def get_assigned_track_ids(user: models.User, db: Session) -> Set[str]:
    """Return the tracks assigned to a judge through either identity link."""
    judge_ids = get_user_judge_identifiers(user, db)
    assignments = db.query(models.JudgeTrack).filter(
        (models.JudgeTrack.user_id == user.id) |
        (models.JudgeTrack.judge_id.in_(judge_ids))
    ).all()
    return {assignment.track_id for assignment in assignments}


@router.get("/api/judge/scores")
def get_judge_scores(
    judge: Optional[str] = None,
    current_user: models.User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """
    Get scores for judges.
    - A judge can only read their own scores.
    - A judge cannot read another judge's scores.
    - Participants cannot access judge scores.
    - Organizers can view scores for any judge or all scores.
    """
    if current_user.role not in ("judge", "organizer"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{current_user.role}' is not authorized to access judge scores",
        )

    if current_user.role == "judge":
        my_ids = get_user_judge_identifiers(current_user, db)
        if judge is not None and judge.strip():
            target_ids = resolve_judge_identifiers(judge, db)
            # Verify the requested judge matches the logged-in judge
            if not (my_ids & target_ids):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="A judge cannot access another judge's scores",
                )
            query_ids = my_ids & target_ids
        else:
            query_ids = my_ids

        scores = db.query(models.Score).filter(
            (models.Score.judge_fixture_id.in_(query_ids)) |
            (models.Score.judge_user_id.in_(query_ids))
        ).all()

    elif current_user.role == "organizer":
        if judge is not None and judge.strip():
            target_ids = resolve_judge_identifiers(judge, db)
            scores = db.query(models.Score).filter(
                (models.Score.judge_fixture_id.in_(target_ids)) |
                (models.Score.judge_user_id.in_(target_ids))
            ).all()
        else:
            scores = db.query(models.Score).all()

    return [
        {
            "id": s.id,
            "judge_id": s.judge_fixture_id or s.judge_user_id,
            "project_id": s.project_id,
            "criteria": s.criteria,
            "comment": s.comment,
            "scored_at": s.scored_at.isoformat() if s.scored_at else None,
        }
        for s in scores
    ]


@router.get("/api/judge/projects")
def get_judge_assigned_projects(
    current_user: models.User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """
    Get projects assigned to the current judge based on track assignment.
    Includes score status and the judge's own score for each project.
    """
    if current_user.role != "judge":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{current_user.role}' is not authorized to view judge projects",
        )

    judge_ids = get_user_judge_identifiers(current_user, db)
    assigned_track_ids = get_assigned_track_ids(current_user, db)

    query = db.query(models.Project).filter(models.Project.status == "submitted")
    query = query.filter(models.Project.track_id.in_(assigned_track_ids))

    projects = query.all()

    # Find scores submitted by this judge
    my_scores = db.query(models.Score).filter(
        (models.Score.judge_fixture_id.in_(judge_ids)) |
        (models.Score.judge_user_id == current_user.id)
    ).all()
    score_by_project = {s.project_id: s for s in my_scores}

    tracks_map = {t.id: t.name for t in db.query(models.Track).all()}

    result = []
    for p in projects:
        s = score_by_project.get(p.id)
        result.append({
            "id": p.id,
            "title": p.title,
            "summary": p.summary,
            "track_id": p.track_id,
            "track_name": tracks_map.get(p.track_id, p.track_id or ""),
            "team_name": p.team.name if p.team else "",
            "repo_url": p.repo_url or "",
            "is_scored": s is not None,
            "my_score": {
                "id": s.id,
                "criteria": s.criteria,
                "comment": s.comment or "",
                "scored_at": s.scored_at.isoformat() if s.scored_at else None,
            } if s else None,
        })
    return result


@router.get("/api/judge/scores/{project_id}")
def get_judge_score_for_project(
    project_id: str,
    current_user: models.User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """
    Get the logged-in judge's score for a specific project.
    Only returns the judge's own score.
    """
    if current_user.role != "judge":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{current_user.role}' is not authorized to access judge scores",
        )

    judge_ids = get_user_judge_identifiers(current_user, db)
    s = db.query(models.Score).filter(
        models.Score.project_id == project_id,
        (models.Score.judge_fixture_id.in_(judge_ids)) |
        (models.Score.judge_user_id == current_user.id)
    ).first()

    if not s:
        return {"is_scored": False, "score": None}

    return {
        "is_scored": True,
        "score": {
            "id": s.id,
            "project_id": s.project_id,
            "criteria": s.criteria,
            "comment": s.comment or "",
            "scored_at": s.scored_at.isoformat() if s.scored_at else None,
        }
    }


@router.post("/api/judge/scores")
def submit_judge_score(
    data: ScoreSubmit,
    current_user: models.User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """
    Submit or update a score for an assigned project.
    Validates required criteria (functionality, quality, innovation: 1-5).
    """
    if current_user.role != "judge":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{current_user.role}' is not authorized to submit scores",
        )

    project = db.query(models.Project).filter(models.Project.id == data.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.status != "submitted":
        raise HTTPException(status_code=400, detail="Cannot score an unsubmitted project")

    if project.track_id not in get_assigned_track_ids(current_user, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Judge is not assigned to this project's track",
        )

    # Validate criteria
    func = data.criteria.get("functionality")
    qual = data.criteria.get("quality")
    innov = data.criteria.get("innovation")
    if func is None or qual is None or innov is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Missing required criteria: functionality, quality, innovation",
        )

    try:
        func, qual, innov = int(func), int(qual), int(innov)
        if not (1 <= func <= 5 and 1 <= qual <= 5 and 1 <= innov <= 5):
            raise ValueError()
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Criteria values must be integers between 1 and 5",
        )

    clean_criteria = {
        "functionality": func,
        "quality": qual,
        "innovation": innov,
    }

    judge_ids = get_user_judge_identifiers(current_user, db)

    # Check for existing score by this judge for this project
    existing = db.query(models.Score).filter(
        models.Score.project_id == data.project_id,
        (models.Score.judge_user_id == current_user.id) |
        (models.Score.judge_fixture_id.in_(judge_ids))
    ).first()

    now = datetime.utcnow()
    if existing:
        existing.criteria = clean_criteria
        existing.comment = data.comment or ""
        existing.judge_user_id = current_user.id
        existing.scored_at = now
        score_record = existing
    else:
        # Determine fixture ID if mapped
        fixture_id = None
        for jid in judge_ids:
            if jid.startswith("jdg_"):
                fixture_id = jid
                break

        score_record = models.Score(
            id=str(uuid.uuid4()),
            judge_fixture_id=fixture_id or current_user.id,
            judge_user_id=current_user.id,
            project_id=data.project_id,
            criteria=clean_criteria,
            comment=data.comment or "",
            scored_at=now,
        )
        db.add(score_record)

    db.commit()
    db.refresh(score_record)
    if project.event_id:
        dispatch_webhook_event(
            db,
            project.event_id,
            "judge.score_submitted",
            "score_submitted",
            score_record.id,
            {
                "score_id": score_record.id,
                "judge_id": current_user.id,
                "project_id": project.id,
                "scored_at": score_record.scored_at.isoformat() if score_record.scored_at else None,
            },
        )

    return {
        "id": score_record.id,
        "judge_id": score_record.judge_fixture_id or score_record.judge_user_id,
        "project_id": score_record.project_id,
        "criteria": score_record.criteria,
        "comment": score_record.comment,
        "scored_at": score_record.scored_at.isoformat() if score_record.scored_at else None,
    }


@router.get("/api/judge/progress")
def get_judging_progress(
    current_user: models.User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    """
    Get judging progress overview for organizers.
    Shows total projects, submitted scores, and judge-level breakdown.
    """
    total_projects = db.query(models.Project).filter(models.Project.status == "submitted").count()
    total_scores = db.query(models.Score).count()

    tracks = db.query(models.Track).all()
    tracks_map = {t.id: t.name for t in tracks}

    judge_users = db.query(models.User).filter(models.User.role == "judge").all()

    judges_progress = []
    for j in judge_users:
        j_ids = get_user_judge_identifiers(j, db)
        jts = db.query(models.JudgeTrack).filter(
            (models.JudgeTrack.user_id == j.id) |
            (models.JudgeTrack.judge_id.in_(j_ids))
        ).all()
        track_ids = list(set(jt.track_id for jt in jts))
        track_names = [tracks_map.get(tid, tid) for tid in track_ids]

        assigned_count = db.query(models.Project).filter(
            models.Project.track_id.in_(track_ids),
            models.Project.status == "submitted"
        ).count() if track_ids else total_projects

        scored_count = db.query(models.Score).filter(
            (models.Score.judge_fixture_id.in_(j_ids)) |
            (models.Score.judge_user_id == j.id)
        ).count()

        pct = round((scored_count / assigned_count * 100), 1) if assigned_count > 0 else 0

        judges_progress.append({
            "id": j.id,
            "name": j.name,
            "email": j.email,
            "tracks": track_names,
            "assigned_projects": assigned_count,
            "scored_projects": scored_count,
            "completion_percentage": min(pct, 100.0),
        })

    track_progress = []
    for t in tracks:
        p_count = db.query(models.Project).filter(models.Project.track_id == t.id, models.Project.status == "submitted").count()
        s_count = db.query(models.Score).join(models.Project).filter(models.Project.track_id == t.id).count()
        track_progress.append({
            "id": t.id,
            "name": t.name,
            "projects_count": p_count,
            "scores_count": s_count,
        })

    return {
        "total_projects": total_projects,
        "total_scores": total_scores,
        "judges": judges_progress,
        "tracks": track_progress,
    }


@router.get("/api/export.csv")
def export_scores_csv(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_organizer),
):
    """
    Export all judging scores as CSV.
    Accessible only to organizers.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id",
        "judge_id",
        "project_id",
        "project_title",
        "track_id",
        "functionality",
        "quality",
        "innovation",
        "comment",
        "scored_at",
    ])

    scores = db.query(models.Score).all()
    for s in scores:
        p = s.project
        criteria = s.criteria if isinstance(s.criteria, dict) else {}
        writer.writerow([
            s.id,
            s.judge_fixture_id or s.judge_user_id or "",
            s.project_id,
            p.title if p else "",
            p.track_id if p else "",
            criteria.get("functionality", ""),
            criteria.get("quality", ""),
            criteria.get("innovation", ""),
            s.comment or "",
            s.scored_at.isoformat() if s.scored_at else "",
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=judging_scores.csv"},
    )
