"""Focused T4 signing utility tests."""
import pytest
import models
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from tests.test_t1 import (
    TestSessionLocal,
    auth_headers,
    client,
    create_event,
    create_token,
    create_user,
    setup_db,
)

from t4_signing import (
    SigningKeyError,
    canonical_json_bytes,
    load_or_create_private_key,
    public_key_info,
    sign_payload,
    verify_payload,
)


def test_ed25519_signature_verifies_and_detects_tampering(tmp_path):
    private_key = load_or_create_private_key(tmp_path / "signing.pem")
    payload = {"event_id": "evt_t4", "subject_id": "seed_jdg_a", "count": 1}
    signed = sign_payload(payload, private_key)

    assert verify_payload(payload, signed["signature"], signed["public_key"])
    tampered = dict(payload, count=2)
    assert not verify_payload(tampered, signed["signature"], signed["public_key"])
    assert not verify_payload(payload, "invalid", signed["public_key"])


def test_canonical_json_is_stable_across_key_order():
    assert canonical_json_bytes({"b": 2, "a": 1}) == canonical_json_bytes({"a": 1, "b": 2})


def test_private_key_persists_and_corrupt_key_is_not_replaced(tmp_path):
    key_path = tmp_path / "signing.pem"
    first = load_or_create_private_key(key_path)
    second = load_or_create_private_key(key_path)
    assert public_key_info(first) == public_key_info(second)

    key_path.write_text("corrupt", encoding="utf-8")
    with pytest.raises(SigningKeyError):
        load_or_create_private_key(key_path)
    assert key_path.read_text(encoding="utf-8") == "corrupt"


@pytest.fixture
def record_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DOGFOOD_SIGNING_KEY_PATH", str(tmp_path / "record-signing.pem"))
    db = TestSessionLocal()
    organizer = create_user(db, "t4org@test.com", "Organizer", "organizer")
    participant = create_user(db, "t4participant@test.com", "Participant", "participant")
    judge = create_user(db, "t4judge@test.com", "Judge", "judge")
    tokens = {
        "organizer": create_token(db, organizer),
        "participant": create_token(db, participant),
        "judge": create_token(db, judge),
    }
    create_event(db, event_id="evt_t4")
    db.add(models.EventRegistration(event_id="evt_t4", user_id=participant.id))
    db.add(models.Team(id="team_t4", name="T4 Team", event_id="evt_t4"))
    db.flush()
    db.add(models.TeamMember(
        team_id="team_t4",
        user_id=participant.id,
        email=participant.email,
    ))
    db.add(models.Project(
        id="project_t4",
        team_id="team_t4",
        track_id="trk_test",
        event_id="evt_t4",
        title="T4 Project",
        status="submitted",
    ))
    db.add(models.JudgeTrack(judge_id="jdg_t4", track_id="trk_test", user_id=judge.id))
    db.flush()
    db.add(models.Score(
        id="score_t4",
        judge_fixture_id="jdg_t4",
        judge_user_id=judge.id,
        project_id="project_t4",
        criteria={"functionality": 5, "quality": 4, "innovation": 5},
    ))
    db.commit()
    db.close()
    return tokens


def test_record_generation_is_organizer_only_and_idempotent(setup_db, record_data):
    url = "/api/events/evt_t4/records/generate"
    assert client.post(url, headers=auth_headers(record_data["participant"])).status_code == 403

    first = client.post(url, headers=auth_headers(record_data["organizer"]))
    second = client.post(url, headers=auth_headers(record_data["organizer"]))
    assert first.status_code == 200
    assert second.status_code == 200
    first_records = first.json()["records"]
    second_records = second.json()["records"]
    assert [record["id"] for record in first_records] == [record["id"] for record in second_records]
    assert {record["payload"]["record_type"] for record in first_records} == {
        "participant_certificate", "judge_participation"
    }


def test_public_record_signature_and_tamper_verification(setup_db, record_data):
    response = client.post(
        "/api/events/evt_t4/records/generate",
        headers=auth_headers(record_data["organizer"]),
    )
    assert response.status_code == 200
    record = next(
        item for item in response.json()["records"]
        if item["payload"]["record_type"] == "judge_participation"
    )
    assert "email" not in str(record).lower()
    assert verify_payload(record["payload"], record["signature"], record["public_key"])

    retrieved = client.get(f"/api/t4/records/{record['id']}")
    assert retrieved.status_code == 200
    assert retrieved.json() == record
    assert client.get(f"/api/t4/records/{record['id']}/verify").json()["valid"] is True

    tampered_payload = dict(record["payload"], data={"scored_project_count": 99})
    verify_response = client.post("/api/t4/records/verify", json={
        "payload": tampered_payload,
        "signature": record["signature"],
        "key_id": record["key_id"],
        "public_key": record["public_key"],
    })
    assert verify_response.status_code == 200
    assert verify_response.json()["valid"] is False


def test_public_key_endpoint_never_exposes_private_key(setup_db, record_data):
    response = client.get("/api/t4/signing/public-key")
    assert response.status_code == 200
    assert response.json()["algorithm"] == "Ed25519"
    assert "private_key" not in response.json()
    assert "PRIVATE KEY" not in response.text


def test_webhook_subscription_is_organizer_only_and_delivers_loopback(setup_db, record_data):
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(202)
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        callback_url = f"http://127.0.0.1:{server.server_port}/hook"
        payload = {
            "target_url": callback_url,
            "event_types": ["event.created", "event.updated"],
        }
        subscriptions_url = "/api/webhooks"
        assert client.post(
            subscriptions_url,
            headers=auth_headers(record_data["participant"]),
            json=payload,
        ).status_code == 403
        subscription = client.post(
            subscriptions_url,
            headers=auth_headers(record_data["organizer"]),
            json=payload,
        )
        assert subscription.status_code == 201
        listed = client.get(
            "/api/webhooks",
            headers=auth_headers(record_data["organizer"]),
        )
        assert listed.status_code == 200
        assert any(item["id"] == subscription.json()["id"] for item in listed.json())
        assert client.get("/api/webhooks").status_code == 401
        assert client.get(
            "/api/webhooks",
            headers=auth_headers(record_data["participant"]),
        ).status_code == 403

        event_create = client.post(
            "/api/events",
            headers=auth_headers(record_data["organizer"]),
            json={"name": "T4 Event Created", "submissions_close": "2027-01-01T00:00:00Z"},
        )
        assert event_create.status_code == 201
        event_id = event_create.json()["id"]
        event_update = client.put(
            f"/api/events/{event_id}",
            headers=auth_headers(record_data["organizer"]),
            json={"name": "T4 Event Updated"},
        )
        assert event_update.status_code == 200
        assert [envelope["event_type"] for envelope in received] == ["event.created", "event.updated"]
        envelope = received[0]
        assert envelope["event_id"] == event_id
        assert envelope["delivery_id"].startswith("whd_")
        assert envelope["timestamp"].endswith("Z")

        deliveries = client.get(
            f"/api/events/{event_id}/webhooks/deliveries",
            headers=auth_headers(record_data["organizer"]),
        )
        assert deliveries.status_code == 200
        assert [delivery["status"] for delivery in deliveries.json()] == ["delivered", "delivered"]
        disabled = client.delete(
            f"/api/webhooks/{subscription.json()['id']}",
            headers=auth_headers(record_data["organizer"]),
        )
        assert disabled.status_code == 200
        assert disabled.json()["active"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_failed_webhook_does_not_break_event_mutation(setup_db, record_data):
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    closed_port = server.server_port
    server.server_close()
    subscription = client.post("/api/webhooks", headers=auth_headers(record_data["organizer"]), json={
        "event_id": "evt_t4",
        "target_url": f"http://127.0.0.1:{closed_port}/unavailable",
        "event_types": ["event.updated"],
    })
    assert subscription.status_code == 201

    update = client.put(
        "/api/events/evt_t4",
        headers=auth_headers(record_data["organizer"]),
        json={"name": "Event remains updated"},
    )
    assert update.status_code == 200
    deliveries = client.get(
        "/api/events/evt_t4/webhooks/deliveries",
        headers=auth_headers(record_data["organizer"]),
    )
    assert deliveries.status_code == 200
    assert deliveries.json()[0]["status"] == "failed"


def test_iframe_gallery_is_public_and_excludes_private_data(setup_db, record_data):
    db = TestSessionLocal()
    db.add(models.Project(
        id="project_t4_draft",
        team_id="team_t4",
        event_id="evt_t4",
        title="PRIVATE DRAFT SENTINEL",
        summary="Must not appear in the public embed.",
        status="draft",
    ))
    db.commit()
    db.close()

    response = client.get("/embed/events/evt_t4/gallery")
    assert response.status_code == 200
    assert "T4 Project" in response.text
    assert "PRIVATE DRAFT SENTINEL" not in response.text
    assert "participant@" not in response.text
    assert "functionality" not in response.text
    assert "votes" not in response.text.lower()


def test_openapi_lists_t4_routes(setup_db, record_data):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    required = {
        "/api/t4/signing/public-key",
        "/api/events/{event_id}/records/generate",
        "/api/t4/records/{record_id}",
        "/api/t4/records/{record_id}/verify",
        "/api/t4/records/verify",
        "/api/webhooks",
        "/api/events/{event_id}/bulk-export",
        "/api/events/bulk-import",
        "/embed/events/{event_id}/gallery",
    }
    assert required.issubset(paths)


def _event_archive():
    return {
        "schema_version": 1,
        "event": {
            "id": "evt_source",
            "name": "Archive Source",
            "description": "Round-trip fixture",
            "submissions_close": "2027-01-01T00:00:00Z",
        },
        "tracks": [],
        "registrations": [],
        "teams": [],
        "projects": [],
        "judge_assignments": [],
        "scores": [],
        "voting_config": None,
    }


def test_bulk_export_is_organizer_only_and_import_rejects_bad_or_protected_ids(setup_db, record_data):
    export_url = "/api/events/evt_t4/bulk-export"
    assert client.get(export_url).status_code == 401
    assert client.get(export_url, headers=auth_headers(record_data["participant"])).status_code == 403
    exported = client.get(export_url, headers=auth_headers(record_data["organizer"]))
    assert exported.status_code == 200
    assert exported.json()["schema_version"] == 1

    import_url = "/api/events/bulk-import"
    headers = auth_headers(record_data["organizer"])
    archive = _event_archive()
    import_request = {"target_event_id": "evt_safe_import", "archive": archive}
    assert client.post("/api/events/bulk-import", json=import_request).status_code == 401
    assert client.post(
        "/api/events/bulk-import",
        headers=auth_headers(record_data["participant"]),
        json=import_request,
    ).status_code == 403
    assert client.post(import_url, headers=headers, json={
        "target_event_id": "evt_safe_import",
        "archive": dict(archive, schema_version=2),
    }).status_code == 422
    assert client.post(import_url, headers=headers, json={
        "target_event_id": "evt_01",
        "archive": archive,
    }).status_code == 409
    protected_source = dict(archive, event=dict(archive["event"], id="evt_t3"))
    assert client.post(import_url, headers=headers, json={
        "target_event_id": "evt_safe_import",
        "archive": protected_source,
    }).status_code == 409
    assert client.post(import_url, headers=headers, json={
        "target_event_id": "evt_safe_import",
        "archive": dict(archive, teams="not-a-list"),
    }).status_code == 422


def test_bulk_export_import_round_trip_preserves_event_data(setup_db, record_data):
    headers = auth_headers(record_data["organizer"])
    exported = client.get("/api/events/evt_t4/bulk-export", headers=headers)
    assert exported.status_code == 200
    archive = exported.json()
    assert archive["event"]["id"] == "evt_t4"
    assert len(archive["tracks"]) == 1
    assert len(archive["registrations"]) == 1
    assert len(archive["teams"]) == 1
    assert len(archive["projects"]) == 1
    assert len(archive["judge_assignments"]) == 1
    assert len(archive["scores"]) == 1

    imported = client.post("/api/events/bulk-import", headers=headers, json={
        "target_event_id": "evt_t4_roundtrip",
        "archive": archive,
    })
    assert imported.status_code == 201, imported.text
    assert imported.json()["projects_imported"] == 1

    round_trip = client.get(
        "/api/events/evt_t4_roundtrip/bulk-export",
        headers=headers,
    )
    assert round_trip.status_code == 200
    result = round_trip.json()
    assert result["event"]["name"] == archive["event"]["name"]
    assert len(result["tracks"]) == len(archive["tracks"])
    assert len(result["registrations"]) == len(archive["registrations"])
    assert len(result["teams"]) == len(archive["teams"])
    assert len(result["teams"][0]["members"]) == 1
    assert result["projects"][0]["title"] == archive["projects"][0]["title"]
    assert result["projects"][0]["status"] == "submitted"
    assert len(result["judge_assignments"]) == len(archive["judge_assignments"])
    assert result["scores"][0]["criteria"] == archive["scores"][0]["criteria"]


def test_existing_tier_routes_remain_available_with_t4(setup_db, record_data):
    assert client.get("/api/projects/gallery").status_code == 200
    assert client.get("/api/events").status_code == 200
    assert client.get("/api/events/evt_t4/voting").status_code == 404
    assert client.get("/api/projects/project_t4/comments").status_code == 200
    assert client.get("/api/judge/scores", headers=auth_headers(record_data["participant"])).status_code == 403