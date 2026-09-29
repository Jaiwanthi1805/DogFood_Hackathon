import json
import os
import sys
import secrets
from datetime import datetime, timezone
from pathlib import Path

# Add src to path for imports when running outside container
sys.path.insert(0, str(Path(__file__).parent))

from database import engine, SessionLocal
import models
from auth import hash_password, make_token
from models import Base

# Fixtures path: /app/data/fixtures.json in Docker, or local data/ outside
_default_fixtures = str(Path(__file__).parent.parent / "data" / "fixtures.json")
FIXTURES_PATH = os.getenv("FIXTURES_PATH", _default_fixtures)

# Deterministic seed tokens (hardcoded so .dogfood.toml can be pre-filled)
SEED_TOKENS = {
    "organizer": "org_token_7f2a9b1c3d4e5f6a7b8c9d0e1f2a3b4c",
    "judge_a":   "jdg_a_token_91bc2d3e4f5a6b7c8d9e0f1a2b3c4d5e",
    "judge_b":   "jdg_b_token_44de5f6a7b8c9d0e1f2a3b4c5d6e7f8a",
    "participant": "prt_token_2e889c0d1e2f3a4b5c6d7e8f9a0b1c2d",
}

# Seed account definitions
SEED_ACCOUNTS = [
    {
        "id": "seed_org_01",
        "email": "organizer@dogfood.local",
        "name": "Organizer",
        "password": "organizer123",
        "role": "organizer",
        "token_key": "organizer",
    },
    {
        "id": "seed_jdg_a",
        "email": "judge_a@dogfood.local",
        "name": "Judge A (Ada Okonkwo / Tomas Varga)",
        "password": "judge_a123",
        "role": "judge",
        "token_key": "judge_a",
        "fixture_judge_id": "jdg_01",  # Tomas Varga
    },
    {
        "id": "seed_jdg_b",
        "email": "judge_b@dogfood.local",
        "name": "Judge B (Wei Lindqvist)",
        "password": "judge_b123",
        "role": "judge",
        "token_key": "judge_b",
        "fixture_judge_id": "jdg_02",  # Wei Lindqvist
    },
    {
        "id": "seed_prt_01",
        "email": "participant@dogfood.local",
        "name": "Participant",
        "password": "participant123",
        "role": "participant",
        "token_key": "participant",
    },
]


def load_fixtures(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def seed_database():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        _seed(db)
    finally:
        db.close()


def print_auth_headers():
    print("\n" + "=" * 60)
    print("DOGFOOD 2026 - Seed complete. Authentication headers:")
    print("=" * 60)
    for key, token in SEED_TOKENS.items():
        print(f'{key} = "Authorization: Bearer {token}"')
    print("=" * 60 + "\n")
    print("Seed accounts:")
    for acc in SEED_ACCOUNTS:
        print(f"  {acc['role']:12s}  {acc['email']}  /  password: {acc['password']}")
    print()


def _seed(db):
    fixtures = load_fixtures(FIXTURES_PATH)

    # Pre-fetch existing records for fast O(1) in-memory checks
    existing_user_ids = set(r[0] for r in db.query(models.User.id).all())
    existing_user_emails = {r[0]: r[1] for r in db.query(models.User.email, models.User.id).all()}
    existing_token_vals = set(r[0] for r in db.query(models.AuthToken.token).all())
    existing_event_ids = set(r[0] for r in db.query(models.Event.id).all())
    existing_registration_pairs = set(
        (r[0], r[1]) for r in db.query(
            models.EventRegistration.event_id,
            models.EventRegistration.user_id,
        ).all()
    )
    existing_track_ids = set(r[0] for r in db.query(models.Track.id).all())
    existing_jt_pairs = set((r[0], r[1]) for r in db.query(models.JudgeTrack.judge_id, models.JudgeTrack.track_id).all())
    existing_team_ids = set(r[0] for r in db.query(models.Team.id).all())
    existing_team_member_pairs = set(
        (r[0], r[1]) for r in db.query(
            models.TeamMember.team_id,
            models.TeamMember.user_id,
        ).filter(models.TeamMember.user_id.isnot(None)).all()
    )
    existing_project_ids = set(r[0] for r in db.query(models.Project.id).all())
    existing_score_pairs = set((r[0], r[1]) for r in db.query(models.Score.judge_fixture_id, models.Score.project_id).all())

    # ── 1. Seed deterministic user accounts ─────────────────────────────────
    for acc in SEED_ACCOUNTS:
        if acc["id"] not in existing_user_ids:
            user = models.User(
                id=acc["id"],
                email=acc["email"],
                name=acc["name"],
                hashed_password=hash_password(acc["password"]),
                role=acc["role"],
                is_seeded=True,
            )
            db.add(user)
            existing_user_ids.add(acc["id"])
            existing_user_emails[acc["email"]] = acc["id"]

        token_value = SEED_TOKENS[acc["token_key"]]
        if token_value not in existing_token_vals:
            # Also check if there's an existing token for this user that's wrong
            wrong_tok = db.query(models.AuthToken).filter(
                models.AuthToken.user_id == acc["id"]
            ).first()
            if wrong_tok and wrong_tok.token != token_value:
                wrong_tok.token = token_value
                existing_token_vals.add(token_value)
            elif not wrong_tok:
                tok = models.AuthToken(token=token_value, user_id=acc["id"])
                db.add(tok)
                existing_token_vals.add(token_value)


    # ── 2. Seed event ────────────────────────────────────────────────────────
    evt_data = fixtures["event"]
    event_id = evt_data["id"]
    if event_id not in existing_event_ids:
        event = models.Event(
            id=evt_data["id"],
            name=evt_data["name"],
            submissions_close=datetime.fromisoformat(
                evt_data["submissions_close"].replace("Z", "+00:00")
            ).replace(tzinfo=None),
            is_seeded=True,
        )
        db.add(event)
        existing_event_ids.add(event_id)

    # ── 2b. Seed isolated T3 voting fixture ─────────────────────────────────
    t3_event_id = "evt_t3"
    if t3_event_id not in existing_event_ids:
        db.add(models.Event(
            id=t3_event_id,
            name="DOGFOOD T3 Community Voting",
            description="Dedicated community-voting acceptance fixture.",
            submissions_close=datetime(2027, 1, 1, 0, 0, 0),
            is_seeded=True,
        ))
        existing_event_ids.add(t3_event_id)

    t3_participant_id = "seed_prt_01"
    t3_registration_pair = (t3_event_id, t3_participant_id)
    if (
        t3_participant_id in existing_user_ids
        and t3_registration_pair not in existing_registration_pairs
    ):
        db.add(models.EventRegistration(
            event_id=t3_event_id,
            user_id=t3_participant_id,
        ))
        existing_registration_pairs.add(t3_registration_pair)

    t3_team_id = "team_t3"
    if t3_team_id not in existing_team_ids:
        db.add(models.Team(
            id=t3_team_id,
            name="T3 Acceptance Team",
            event_id=t3_event_id,
            is_seeded=True,
        ))
        existing_team_ids.add(t3_team_id)

    t3_projects = [
        {
            "id": "project_t3_a",
            "title": "Alpha",
            "summary": "First seeded community-voting project.",
            "repo_url": "https://example.org/dogfood/t3-alpha",
        },
        {
            "id": "project_t3_b",
            "title": "Beta",
            "summary": "Second seeded community-voting project.",
            "repo_url": "https://example.org/dogfood/t3-beta",
        },
    ]
    for project_data in t3_projects:
        if project_data["id"] not in existing_project_ids:
            db.add(models.Project(
                id=project_data["id"],
                team_id=t3_team_id,
                event_id=t3_event_id,
                title=project_data["title"],
                summary=project_data["summary"],
                repo_url=project_data["repo_url"],
                status="submitted",
                submitted_at=datetime(2026, 9, 1, 12, 0, 0),
                is_seeded=True,
            ))
            existing_project_ids.add(project_data["id"])

    # ── 2c. Seed isolated T4 archive/signing fixture ──────────────────────────
    t4_event_id = "evt_t4"
    if t4_event_id not in existing_event_ids:
        db.add(models.Event(
            id=t4_event_id,
            name="DOGFOOD T4 Stretch Acceptance",
            description="Dedicated fixture for T4 records, webhooks, widget, and archives.",
            submissions_close=datetime(2027, 1, 1, 0, 0, 0),
            is_seeded=True,
        ))
        existing_event_ids.add(t4_event_id)

    t4_registration_pair = (t4_event_id, "seed_prt_01")
    if (
        "seed_prt_01" in existing_user_ids
        and t4_registration_pair not in existing_registration_pairs
    ):
        db.add(models.EventRegistration(event_id=t4_event_id, user_id="seed_prt_01"))
        existing_registration_pairs.add(t4_registration_pair)

    t4_track_id = "trk_t4"
    if t4_track_id not in existing_track_ids:
        db.add(models.Track(id=t4_track_id, event_id=t4_event_id, name="T4 Acceptance Track"))
        existing_track_ids.add(t4_track_id)

    t4_team_id = "team_t4"
    if t4_team_id not in existing_team_ids:
        db.add(models.Team(
            id=t4_team_id,
            name="T4 Acceptance Team",
            event_id=t4_event_id,
            is_seeded=True,
        ))
        existing_team_ids.add(t4_team_id)
    if (t4_team_id, "seed_prt_01") not in existing_team_member_pairs:
        db.add(models.TeamMember(
            team_id=t4_team_id,
            user_id="seed_prt_01",
            email="participant@dogfood.local",
        ))
        existing_team_member_pairs.add((t4_team_id, "seed_prt_01"))

    t4_project_id = "project_t4_a"
    if t4_project_id not in existing_project_ids:
        db.add(models.Project(
            id=t4_project_id,
            team_id=t4_team_id,
            track_id=t4_track_id,
            event_id=t4_event_id,
            title="T4 Reference Project",
            summary="Stable submitted project for T4 endpoint and archive tests.",
            repo_url="https://example.org/dogfood/t4-reference",
            status="submitted",
            submitted_at=datetime(2026, 9, 1, 12, 0, 0),
            is_seeded=True,
        ))
        existing_project_ids.add(t4_project_id)

    t4_judge_pair = ("jdg_01", t4_track_id)
    if t4_judge_pair not in existing_jt_pairs:
        db.add(models.JudgeTrack(
            judge_id="jdg_01",
            track_id=t4_track_id,
            user_id="seed_jdg_a",
        ))
        existing_jt_pairs.add(t4_judge_pair)

    if ("jdg_01", t4_project_id) not in existing_score_pairs:
        db.add(models.Score(
            id="score_t4_jdg_a_project_a",
            judge_fixture_id="jdg_01",
            judge_user_id="seed_jdg_a",
            project_id=t4_project_id,
            criteria={"functionality": 5, "quality": 4, "innovation": 5},
            comment="T4 seeded judge participation record.",
        ))
        existing_score_pairs.add(("jdg_01", t4_project_id))

    # ── 3. Seed tracks ───────────────────────────────────────────────────────
    for trk_data in fixtures["tracks"]:
        if trk_data["id"] not in existing_track_ids:
            track = models.Track(
                id=trk_data["id"],
                event_id=event_id,
                name=trk_data["name"],
            )
            db.add(track)
            existing_track_ids.add(trk_data["id"])

    # ── 4. Seed judge-track assignments (for T2 readiness) ──────────────────
    for jdg_data in fixtures["judges"]:
        for track_id in jdg_data.get("tracks", []):
            if (jdg_data["id"], track_id) not in existing_jt_pairs:
                linked_user_id = None
                for acc in SEED_ACCOUNTS:
                    if acc.get("fixture_judge_id") == jdg_data["id"]:
                        linked_user_id = acc["id"]
                jt = models.JudgeTrack(
                    judge_id=jdg_data["id"],
                    track_id=track_id,
                    user_id=linked_user_id,
                )
                db.add(jt)
                existing_jt_pairs.add((jdg_data["id"], track_id))

    # ── 5. Seed teams & member users ────────────────────────────────────────
    all_member_emails = set()
    for tm_data in fixtures["teams"]:
        for email in tm_data.get("members", []):
            all_member_emails.add(email)

    for email in all_member_emails:
        if email not in existing_user_emails:
            uid = f"fixture_user_{email.replace('@', '_at_').replace('.', '_')}"
            user = models.User(
                id=uid,
                email=email,
                name=email.split("@")[0],
                hashed_password=hash_password("fixture_password"),
                role="participant",
                is_seeded=True,
            )
            db.add(user)
            existing_user_emails[email] = uid
            existing_user_ids.add(uid)

    for tm_data in fixtures["teams"]:
        if tm_data["id"] not in existing_team_ids:
            team = models.Team(
                id=tm_data["id"],
                name=tm_data["name"],
                event_id=event_id,
                is_seeded=True,
            )
            db.add(team)
            existing_team_ids.add(tm_data["id"])

            for email in tm_data.get("members", []):
                uid = existing_user_emails.get(email)
                member = models.TeamMember(
                    team_id=tm_data["id"],
                    user_id=uid,
                    email=email,
                )
                db.add(member)

    # ── 6. Seed projects ─────────────────────────────────────────────────────
    for prj_data in fixtures["projects"]:
        existing = db.query(models.Project).filter(
            models.Project.id == prj_data["id"]
        ).first()
        if not existing:
            submitted_at = datetime.fromisoformat(
                prj_data["submitted_at"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
            project = models.Project(
                id=prj_data["id"],
                team_id=prj_data["team"],
                track_id=prj_data["track"],
                event_id=event.id,
                title=prj_data["title"],
                summary=prj_data.get("summary", ""),
                repo_url=prj_data.get("repo_url", ""),
                status="submitted",
                submitted_at=submitted_at,
                is_seeded=True,
            )
            db.add(project)

    db.flush()

    # ── 7. Seed scores (preserve for T2) ────────────────────────────────────
    for score_data in fixtures["scores"]:
        existing = db.query(models.Score).filter(
            models.Score.judge_fixture_id == score_data["judge"],
            models.Score.project_id == score_data["project"],
        ).first()
        if not existing:
            linked_user_id = None
            for acc in SEED_ACCOUNTS:
                if acc.get("fixture_judge_id") == score_data["judge"]:
                    linked_user_id = acc["id"]
            score = models.Score(
                judge_fixture_id=score_data["judge"],
                judge_user_id=linked_user_id,
                project_id=score_data["project"],
                criteria=score_data.get("criteria", {}),
                comment=score_data.get("comment", ""),
            )
            db.add(score)

    db.commit()

    # ── 8. Print auth headers ────────────────────────────────────────────────
    print_auth_headers()


if __name__ == "__main__":
    seed_database()
    print("Seeding complete.")
