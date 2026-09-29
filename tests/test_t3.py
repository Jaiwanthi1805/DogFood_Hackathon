"""Focused T3 community voting and comment tests."""
from datetime import datetime, timedelta

import pytest
import models
from routers import community
from tests.test_t1 import (
    TestSessionLocal,
    auth_headers,
    client,
    create_event,
    create_token,
    create_user,
    setup_db,
)


@pytest.fixture
def voting_data():
    db = TestSessionLocal()
    organizer = create_user(db, "t3org@test.com", "Organizer", "organizer")
    participant = create_user(db, "t3participant@test.com", "Participant", "participant")
    unregistered = create_user(db, "t3other@test.com", "Other", "participant")
    judge = create_user(db, "t3judge@test.com", "Judge", "judge")
    tokens = {
        "organizer": create_token(db, organizer),
        "participant": create_token(db, participant),
        "unregistered": create_token(db, unregistered),
        "judge": create_token(db, judge),
    }
    create_event(db, event_id="evt_t3")
    db.add(models.EventRegistration(event_id="evt_t3", user_id=participant.id))
    db.add(models.Team(id="team_t3", name="Team T3", event_id="evt_t3"))
    db.add_all([
        models.Project(id="project_t3_a", team_id="team_t3", event_id="evt_t3", title="Alpha", status="submitted"),
        models.Project(id="project_t3_b", team_id="team_t3", event_id="evt_t3", title="Beta", status="submitted"),
    ])
    db.commit()
    db.close()
    return tokens


def set_voting(tokens, *, starts_at=None, ends_at=None, hide_results=True, eligibility="registered_participants", enabled=True):
    now = datetime.utcnow()
    return client.put(
        "/api/events/evt_t3/voting",
        headers=auth_headers(tokens["organizer"]),
        json={
            "enabled": enabled,
            "eligibility": eligibility,
            "starts_at": (starts_at or now - timedelta(minutes=1)).isoformat() + "Z",
            "ends_at": (ends_at or now + timedelta(minutes=10)).isoformat() + "Z",
            "hide_results": hide_results,
        },
    )


def test_voting_configuration_is_organizer_only_and_validates_window(setup_db, voting_data):
    payload = {
        "enabled": True,
        "eligibility": "registered_participants",
        "starts_at": "2026-10-01T00:00:00Z",
        "ends_at": "2026-10-02T00:00:00Z",
        "hide_results": True,
    }
    response = client.put(
        "/api/events/evt_t3/voting",
        headers=auth_headers(voting_data["participant"]),
        json=payload,
    )
    assert response.status_code == 403

    payload["starts_at"], payload["ends_at"] = payload["ends_at"], payload["starts_at"]
    response = client.put(
        "/api/events/evt_t3/voting",
        headers=auth_headers(voting_data["organizer"]),
        json=payload,
    )
    assert response.status_code == 422


def test_vote_requires_registered_participant_and_open_window(setup_db, voting_data):
    set_voting(voting_data)
    path = "/api/events/evt_t3/voting/votes"
    payload = {"project_id": "project_t3_a"}

    assert client.post(path, json=payload).status_code == 401
    assert client.post(path, headers=auth_headers(voting_data["judge"]), json=payload).status_code == 403
    assert client.post(path, headers=auth_headers(voting_data["unregistered"]), json=payload).status_code == 403

    for starts_at, ends_at in (
        (datetime.utcnow() + timedelta(hours=1), datetime.utcnow() + timedelta(hours=2)),
        (datetime.utcnow() - timedelta(hours=2), datetime.utcnow() - timedelta(hours=1)),
    ):
        set_voting(voting_data, starts_at=starts_at, ends_at=ends_at)
        response = client.post(path, headers=auth_headers(voting_data["participant"]), json=payload)
        assert response.status_code == 409

    audit = client.get("/api/events/evt_t3/voting/audit", headers=auth_headers(voting_data["organizer"]))
    actions = [record["action"] for record in audit.json()]
    assert actions.count("vote_rejected_ineligible") == 2
    assert actions.count("vote_rejected_window") == 2


def test_duplicate_vote_is_rejected_and_audited(setup_db, voting_data):
    set_voting(voting_data)
    path = "/api/events/evt_t3/voting/votes"
    headers = auth_headers(voting_data["participant"])
    assert client.post(path, headers=headers, json={"project_id": "project_t3_a"}).status_code == 201
    duplicate = client.post(path, headers=headers, json={"project_id": "project_t3_b"})
    assert duplicate.status_code == 409

    db = TestSessionLocal()
    assert db.query(models.CommunityVote).filter_by(event_id="evt_t3").count() == 1
    db.close()

    audit = client.get("/api/events/evt_t3/voting/audit", headers=auth_headers(voting_data["organizer"]))
    assert audit.status_code == 200
    actions = [record["action"] for record in audit.json()]
    assert "vote_cast" in actions
    assert "duplicate_vote_rejected" in actions
    assert "email" not in str(audit.json()).lower()


def test_active_results_are_hidden_from_non_organizers(setup_db, voting_data):
    set_voting(voting_data)
    path = "/api/events/evt_t3/voting/results"
    assert client.get(path).status_code == 403
    assert client.get(path, headers=auth_headers(voting_data["participant"])).status_code == 403
    organizer_result = client.get(path, headers=auth_headers(voting_data["organizer"]))
    assert organizer_result.status_code == 200
    assert len(organizer_result.json()["results"]) == 2

    set_voting(voting_data, hide_results=False)
    assert client.get(path).status_code == 200


def test_disabled_voting_rejects_project_list_and_votes(setup_db, voting_data):
    set_voting(voting_data, enabled=False)
    headers = auth_headers(voting_data["participant"])
    projects = client.get("/api/events/evt_t3/voting/projects", headers=headers)
    vote = client.post(
        "/api/events/evt_t3/voting/votes",
        headers=headers,
        json={"project_id": "project_t3_a"},
    )
    assert projects.status_code == 409
    assert vote.status_code == 409


def test_results_become_public_after_hidden_voting_closes(setup_db, voting_data):
    set_voting(voting_data)
    vote = client.post(
        "/api/events/evt_t3/voting/votes",
        headers=auth_headers(voting_data["participant"]),
        json={"project_id": "project_t3_a"},
    )
    assert vote.status_code == 201

    set_voting(
        voting_data,
        starts_at=datetime.utcnow() - timedelta(hours=2),
        ends_at=datetime.utcnow() - timedelta(hours=1),
        hide_results=True,
    )
    response = client.get("/api/events/evt_t3/voting/results")
    assert response.status_code == 200
    results = {result["project_id"]: result["votes"] for result in response.json()["results"]}
    assert results["project_t3_a"] == 1


def test_voting_endpoint_rate_limits_attempts_and_audits_limit(setup_db, voting_data):
    set_voting(voting_data)
    path = "/api/events/evt_t3/voting/votes"
    headers = auth_headers(voting_data["participant"])
    payload = {"project_id": "project_t3_a"}
    statuses = [client.post(path, headers=headers, json=payload).status_code for _ in range(6)]
    assert statuses == [201, 409, 409, 409, 409, 429]

    audit = client.get("/api/events/evt_t3/voting/audit", headers=auth_headers(voting_data["organizer"]))
    assert "vote_rate_limited" in [record["action"] for record in audit.json()]


def test_voter_project_list_is_randomized_and_results_are_not_exposed(setup_db, voting_data, monkeypatch):
    set_voting(voting_data)

    class ReverseOrder:
        def shuffle(self, projects):
            projects.reverse()

    monkeypatch.setattr(community.secrets, "SystemRandom", lambda: ReverseOrder())
    response = client.get(
        "/api/events/evt_t3/voting/projects",
        headers=auth_headers(voting_data["participant"]),
    )
    assert response.status_code == 200
    assert [project["id"] for project in response.json()] == ["project_t3_b", "project_t3_a"]
    assert all("votes" not in project for project in response.json())


def test_judge_and_organizer_cannot_cast_community_votes(setup_db, voting_data):
    set_voting(voting_data)
    path = "/api/events/evt_t3/voting/votes"
    payload = {"project_id": "project_t3_a"}
    assert client.post(path, headers=auth_headers(voting_data["judge"]), json=payload).status_code == 403
    assert client.post(path, headers=auth_headers(voting_data["organizer"]), json=payload).status_code == 403


def test_all_participant_mode_allows_unregistered_participant(setup_db, voting_data):
    set_voting(voting_data, eligibility="all_participants")
    response = client.post(
        "/api/events/evt_t3/voting/votes",
        headers=auth_headers(voting_data["unregistered"]),
        json={"project_id": "project_t3_a"},
    )
    assert response.status_code == 201


def test_voting_audit_is_organizer_only(setup_db, voting_data):
    response = client.get(
        "/api/events/evt_t3/voting/audit",
        headers=auth_headers(voting_data["participant"]),
    )
    assert response.status_code == 403


def test_comments_are_public_to_read_and_participant_only_to_write(setup_db, voting_data):
    path = "/api/projects/project_t3_a/comments"
    assert client.get(path).status_code == 200
    assert client.post(path, json={"body": "A comment"}).status_code == 401
    assert client.post(path, headers=auth_headers(voting_data["judge"]), json={"body": "A comment"}).status_code == 403
    assert client.post(path, headers=auth_headers(voting_data["organizer"]), json={"body": "A comment"}).status_code == 403

    response = client.post(
        path,
        headers=auth_headers(voting_data["participant"]),
        json={"body": "  Thoughtful project.  "},
    )
    assert response.status_code == 201
    assert response.json()["body"] == "Thoughtful project."
    assert response.json()["author"] == "Participant"
    comments = client.get(path).json()
    assert len(comments) == 1
    assert "email" not in str(comments).lower()

    audit = client.get("/api/events/evt_t3/voting/audit", headers=auth_headers(voting_data["organizer"]))
    actions = [record["action"] for record in audit.json()]
    assert actions.count("comment_rejected_unauthorized") == 2
    assert "comment_posted" in actions


def test_comment_content_is_validated(setup_db, voting_data):
    path = "/api/projects/project_t3_a/comments"
    headers = auth_headers(voting_data["participant"])
    assert client.post(path, headers=headers, json={"body": "   "}).status_code == 422
    assert client.post(path, headers=headers, json={"body": "x" * 1001}).status_code == 422

    audit = client.get("/api/events/evt_t3/voting/audit", headers=auth_headers(voting_data["organizer"]))
    assert "comment_rejected_validation" in [record["action"] for record in audit.json()]


def test_voting_and_comment_pages_render(setup_db, voting_data):
    assert client.get("/events/evt_t3/vote").status_code == 200
    event_page = client.get("/events/evt_t3")
    assert event_page.status_code == 200
    assert "voting-config-form" in event_page.text
    response = client.get("/projects/project_t3_a")
    assert response.status_code == 200
    assert "Community comments" in response.text


@pytest.mark.parametrize(
    "title,repo_url",
    [
        (" alpha  ", "https://github.com/other/repo"),
        ("A different title", "https://github.com/same/project/"),
    ],
)
def test_duplicate_project_submission_is_rejected_and_audited(setup_db, voting_data, title, repo_url):
    db = TestSessionLocal()
    db.query(models.Project).filter_by(id="project_t3_a").update(
        {models.Project.repo_url: "https://github.com/same/project"}
    )
    db.commit()
    db.close()

    headers = auth_headers(voting_data["participant"])
    draft = client.post(
        "/api/projects",
        headers=headers,
        json={"title": title, "repo_url": repo_url, "event_id": "evt_t3"},
    )
    assert draft.status_code == 201
    response = client.post(f"/api/projects/{draft.json()['id']}/submit", headers=headers)
    assert response.status_code == 409

    audit = client.get("/api/events/evt_t3/voting/audit", headers=auth_headers(voting_data["organizer"]))
    assert "duplicate_project_rejected" in [record["action"] for record in audit.json()]