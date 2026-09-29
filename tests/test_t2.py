"""
T2 Tests for the DOGFOOD 2026 portal.
Tests judging score access isolation and CSV export.
"""
import pytest
import models
from tests.test_t1 import client, TestSessionLocal, create_user, create_token, auth_headers, setup_db


@pytest.fixture
def judging_data():
    db = TestSessionLocal()

    org = create_user(db, "organizer@dogfood.local", "Organizer", "organizer")
    jdg_a = create_user(db, "judge_a@dogfood.local", "Judge A", "judge")
    jdg_b = create_user(db, "judge_b@dogfood.local", "Judge B", "judge")
    prt = create_user(db, "participant@dogfood.local", "Participant", "participant")

    tok_org = create_token(db, org)
    tok_a = create_token(db, jdg_a)
    tok_b = create_token(db, jdg_b)
    tok_prt = create_token(db, prt)

    trk = models.Track(id="trk_01", event_id="evt_01", name="Track 1")
    team = models.Team(id="tm_01", name="Team 1", event_id="evt_01")
    prj = models.Project(id="prj_01", team_id="tm_01", track_id="trk_01", event_id="evt_01", title="Project 1", status="submitted")
    db.add_all([trk, team, prj])

    jt_a = models.JudgeTrack(judge_id="jdg_01", track_id="trk_01", user_id=jdg_a.id)
    jt_b = models.JudgeTrack(judge_id="jdg_02", track_id="trk_01", user_id=jdg_b.id)
    db.add_all([jt_a, jt_b])

    s_a = models.Score(
        judge_fixture_id="jdg_01",
        judge_user_id=jdg_a.id,
        project_id="prj_01",
        criteria={"functionality": 5, "quality": 4, "innovation": 5},
        comment="Great job",
    )
    s_b = models.Score(
        judge_fixture_id="jdg_02",
        judge_user_id=jdg_b.id,
        project_id="prj_01",
        criteria={"functionality": 3, "quality": 3, "innovation": 2},
        comment="Okay",
    )
    db.add_all([s_a, s_b])
    db.commit()

    tokens = {
        "org": tok_org,
        "judge_a": tok_a,
        "judge_b": tok_b,
        "participant": tok_prt,
    }
    db.close()
    return tokens


def test_judge_can_access_own_scores(judging_data):
    resp = client.get("/api/judge/scores", headers=auth_headers(judging_data["judge_a"]))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["project_id"] == "prj_01"
    assert data[0]["criteria"]["functionality"] == 5


def test_judge_can_load_assigned_projects_and_score_them(judging_data):
    db = TestSessionLocal()
    project = db.query(models.Project).filter_by(id="prj_01").first()
    project.track_id = "trk_01"
    db.commit()
    db.close()

    projects = client.get(
        "/api/judge/projects",
        headers=auth_headers(judging_data["judge_a"]),
    )
    assert projects.status_code == 200
    assert [project["id"] for project in projects.json()] == ["prj_01"]

    score = client.post(
        "/api/judge/scores",
        headers=auth_headers(judging_data["judge_a"]),
        json={
            "project_id": "prj_01",
            "criteria": {"functionality": 4, "quality": 4, "innovation": 5},
            "comment": "Assigned project",
        },
    )
    assert score.status_code == 200
    assert score.json()["criteria"]["innovation"] == 5


def test_judge_cannot_score_unassigned_project(judging_data):
    db = TestSessionLocal()
    db.add(models.Track(id="trk_02", event_id="evt_01", name="Track 2"))
    project = db.query(models.Project).filter_by(id="prj_01").first()
    project.track_id = "trk_02"
    db.commit()
    db.close()

    response = client.post(
        "/api/judge/scores",
        headers=auth_headers(judging_data["judge_a"]),
        json={
            "project_id": "prj_01",
            "criteria": {"functionality": 4, "quality": 4, "innovation": 4},
        },
    )
    assert response.status_code == 403


def test_judge_can_access_own_scores_with_param(judging_data):
    resp = client.get("/api/judge/scores?judge=judge_a", headers=auth_headers(judging_data["judge_a"]))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1


def test_judge_cannot_access_peer_scores(judging_data):
    resp = client.get("/api/judge/scores?judge=judge_a", headers=auth_headers(judging_data["judge_b"]))
    assert resp.status_code == 403


def test_participant_cannot_access_judge_scores(judging_data):
    resp = client.get("/api/judge/scores", headers=auth_headers(judging_data["participant"]))
    assert resp.status_code == 403


def test_unauthenticated_cannot_access_judge_scores(judging_data):
    resp = client.get("/api/judge/scores")
    assert resp.status_code == 401


def test_organizer_can_access_judge_scores(judging_data):
    resp = client.get("/api/judge/scores", headers=auth_headers(judging_data["org"]))
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_organizer_can_access_specific_judge_scores(judging_data):
    resp = client.get("/api/judge/scores?judge=judge_a", headers=auth_headers(judging_data["org"]))
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_organizer_can_export_csv(judging_data):
    resp = client.get("/api/export.csv", headers=auth_headers(judging_data["org"]))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert "judging_scores.csv" in resp.headers["content-disposition"]
    text = resp.text
    assert "score_id" in text or "id" in text
    assert "functionality" in text
    assert "Project 1" in text
    assert "," in text and "\n" in text


def test_participant_cannot_export_csv(judging_data):
    resp = client.get("/api/export.csv", headers=auth_headers(judging_data["participant"]))
    assert resp.status_code == 403


def test_judge_cannot_export_csv(judging_data):
    resp = client.get("/api/export.csv", headers=auth_headers(judging_data["judge_a"]))
    assert resp.status_code == 403


def test_unauthenticated_cannot_export_csv(judging_data):
    resp = client.get("/api/export.csv")
    assert resp.status_code == 401
