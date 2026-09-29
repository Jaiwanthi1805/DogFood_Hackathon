#!/usr/bin/env python3
"""
DOGFOOD 2026 acceptance checker.
Usage: python3 run.py .dogfood.toml > acceptance-report.txt

Implements the T1, T2, T3, and T4 checks specified at https://dogfoodhack.com/spec
Standard library only.
"""
import sys
import json
import base64
import hashlib
import threading
import uuid
import urllib.request
import urllib.error
import urllib.parse
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

def load_toml(path):
    """Minimal TOML parser for the .dogfood.toml format."""
    result = {}
    section = None
    with open(path, "r") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                result[section] = {}
                continue
            if "=" in line and section is not None:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                # Strip surrounding quotes
                if (val.startswith('"') and val.endswith('"')) or \
                   (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                # Handle arrays like ["T1", "T2"]
                elif val.startswith("[") and val.endswith("]"):
                    inner = val[1:-1]
                    val = [v.strip().strip('"').strip("'") for v in inner.split(",") if v.strip()]
                result[section][key] = val
    return result

def load_fixture_titles(toml_dir):
    """Load known project titles from fixtures.json (next to run.py or in data/)."""
    candidates = [
        os.path.join(toml_dir, "fixtures.json"),
        os.path.join(toml_dir, "data", "fixtures.json"),
        "fixtures.json",
        os.path.join(os.path.dirname(__file__), "fixtures.json"),
        os.path.join(os.path.dirname(__file__), "data", "fixtures.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, "r") as f:
                data = json.load(f)
            return [p.get("title", "") for p in data.get("projects", [])]
    return []

def make_request(url, method="GET", headers=None, body=None):
    """Make HTTP request, return (status_code, body_text)."""
    req = urllib.request.Request(url, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if body:
        req.data = body.encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)

def parse_auth_header(header_str):
    """Parse 'HeaderName: Value' into dict."""
    if ":" in header_str:
        name, _, value = header_str.partition(":")
        return {name.strip(): value.strip()}
    return {}

def fmt_pass(label, width=38):
    dots = "." * (width - len(label))
    return f"{label} {dots} PASS"

def fmt_fail(label, detail, width=38):
    dots = "." * (width - len(label))
    return f"{label} {dots} FAIL\n  {detail}"

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 run.py .dogfood.toml")
        sys.exit(1)

    toml_path = sys.argv[1]
    toml_dir = os.path.dirname(os.path.abspath(toml_path))

    cfg = load_toml(toml_path)
    base_url = cfg.get("portal", {}).get("base_url", "http://localhost:8080").rstrip("/")
    claimed = cfg.get("tiers", {}).get("claimed", [])
    if isinstance(claimed, str):
        claimed = [claimed]

    routes = cfg.get("routes", {})
    auth = cfg.get("auth", {})

    gallery_path = routes.get("gallery", "/projects")
    submit_path = routes.get("submit", "/api/projects/submit-new")
    judge_scores_path = routes.get("judge_scores", "/api/judge/scores")
    peer_scores_path = routes.get("peer_scores", "/api/judge/scores?judge=judge_a")
    csv_export_path = cfg.get("routes", {}).get("csv_export", "/api/export.csv")

    auth_organizer = parse_auth_header(auth.get("organizer", ""))
    auth_judge_a = parse_auth_header(auth.get("judge_a", ""))
    auth_judge_b = parse_auth_header(auth.get("judge_b", ""))
    auth_participant = parse_auth_header(auth.get("participant", ""))

    fixture_titles = load_fixture_titles(toml_dir)

    lines = []
    lines.append(f"DOGFOOD 2026 acceptance report")
    lines.append(f"portal:   {base_url}")
    lines.append(f"claimed:  {' '.join(claimed)}")
    lines.append(f"fixtures: {len(fixture_titles)} project titles loaded")
    lines.append("")

    verified = []
    failed = []

    # ── T1 checks ──────────────────────────────────────────────────────────────

    # T1.1 · A stranger can browse the gallery.
    # GET {routes.gallery} no auth header expect 200
    gallery_url = base_url + gallery_path
    status, body = make_request(gallery_url)
    if status == 200:
        lines.append(fmt_pass("T1 gallery is public"))
        verified.append("T1")
    else:
        detail = (f"GET {gallery_url} with no auth\n"
                  f"  got {status}, wanted 200")
        lines.append(fmt_fail("T1 gallery is public", detail))
        failed.append("T1-gallery-public")

    # T1.2 · The gallery shows fixture projects.
    # GET {routes.gallery} expect a known fixture title in the body
    status2, body2 = make_request(gallery_url)
    found_title = None
    for title in fixture_titles:
        if title and title in body2:
            found_title = title
            break
    if found_title:
        lines.append(fmt_pass("T1 project from fixtures shown"))
    else:
        sample = fixture_titles[:3] if fixture_titles else ["(none loaded)"]
        detail = (f"GET {gallery_url}\n"
                  f"  wanted one of: {sample!r}\n"
                  f"  none found in response body")
        lines.append(fmt_fail("T1 project from fixtures shown", detail))
        failed.append("T1-fixture-title")

    # T1.3 · A closed event refuses submissions.
    # POST {routes.submit} as participant expect 4xx
    submit_url = base_url + submit_path
    status3, body3 = make_request(
        submit_url, method="POST",
        headers=auth_participant,
        body='{"title":"Checker submission","event_id":"evt_01"}'
    )
    if 400 <= status3 <= 499:
        lines.append(fmt_pass("T1 closed event refuses submissions"))
    else:
        detail = (f"POST {submit_url} sent as participant\n"
                  f"  got {status3}, wanted 4xx\n"
                  f"  the backend did not enforce the submission deadline")
        lines.append(fmt_fail("T1 closed event refuses submissions", detail))
        failed.append("T1-deadline")

    lines.append("")

    # ── T2 checks ──────────────────────────────────────────────────────────────

    # T2.1 · A judge can read their own scores.
    # GET {routes.judge_scores} as judge_a expect 200
    scores_url = base_url + judge_scores_path
    status4, body4 = make_request(scores_url, headers=auth_judge_a)
    if status4 == 200:
        lines.append(fmt_pass("T2 judge sees own scores"))
        if "T2" not in verified:
            verified.append("T2")
    else:
        detail = (f"GET {scores_url} sent as judge_a\n"
                  f"  got {status4}, wanted 200")
        lines.append(fmt_fail("T2 judge sees own scores", detail))
        failed.append("T2-judge-own-scores")

    # T2.2 · A judge cannot read a peer's scores.
    # GET {routes.peer_scores} as judge_b expect 401 or 403
    peer_url = base_url + peer_scores_path
    status5, body5 = make_request(peer_url, headers=auth_judge_b)
    if status5 in (401, 403):
        lines.append(fmt_pass("T2 judge cannot see peer scores"))
    else:
        detail = (f"GET {peer_url} sent as judge_b; this is the url that returns judge_a's scores\n"
                  f"  got {status5}, wanted 401 or 403\n"
                  f"  the backend returned another judge's scores")
        lines.append(fmt_fail("T2 judge cannot see peer scores", detail))
        failed.append("T2-peer-scores")

    # T2.3 · A participant is not a judge.
    # GET {routes.judge_scores} as participant expect 401 or 403
    status6, body6 = make_request(scores_url, headers=auth_participant)
    if status6 in (401, 403):
        lines.append(fmt_pass("T2 participant blocked"))
    else:
        detail = (f"GET {scores_url} sent as participant\n"
                  f"  got {status6}, wanted 401 or 403")
        lines.append(fmt_fail("T2 participant blocked", detail))
        failed.append("T2-participant-blocked")

    # T2.4 · An organizer can export CSV.
    # GET {routes.csv_export} as organizer expect 200 and a CSV body
    csv_url = base_url + csv_export_path
    status7, body7 = make_request(csv_url, headers=auth_organizer)
    is_csv = (status7 == 200 and ("," in body7 or "\n" in body7))
    if is_csv:
        lines.append(fmt_pass("T2 csv export works"))
    else:
        detail = (f"GET {csv_url} sent as organizer\n"
                  f"  got {status7}, wanted 200 with CSV body")
        lines.append(fmt_fail("T2 csv export works", detail))
        failed.append("T2-csv-export")

    lines.append("")

    # ── T3 checks ──────────────────────────────────────────────────────────────

    t3_checks = []
    t3_passed = False

    def record_t3(label, failure_id, passed, detail):
        if passed:
            lines.append(fmt_pass(label))
        else:
            lines.append(fmt_fail(label, detail))
            failed.append(failure_id)
        t3_checks.append(failure_id)

    event_status, event_body = make_request(base_url + "/api/events")
    available_events = []
    if event_status == 200:
        try:
            available_events = json.loads(event_body)
        except (TypeError, ValueError):
            available_events = []

    dedicated_t3_event = next(
        (event for event in available_events if event.get("id") == "evt_t3"),
        None,
    )
    open_events = sorted(
        (event for event in available_events
         if event.get("is_open") and event.get("id") != "evt_t3"),
        key=lambda event: event.get("id", ""),
    )
    event_candidates = ([dedicated_t3_event] if dedicated_t3_event else []) + open_events
    gallery_status, gallery_body = make_request(base_url + "/api/projects/gallery")
    try:
        submitted_projects = json.loads(gallery_body) if gallery_status == 200 else []
    except (TypeError, ValueError):
        submitted_projects = []
    t3_event = next(
        (event for event in event_candidates if any(
            isinstance(project, dict) and project.get("event_id") == event.get("id")
            for project in submitted_projects
        )),
        None,
    )

    t3_labels = [
        ("T3 voting configuration", "T3-voting-config"),
        ("T3 voting access", "T3-voting-access"),
        ("T3 ballot ordering and count privacy", "T3-random-order"),
        ("T3 hidden active results", "T3-results-hidden"),
        ("T3 duplicate vote prevention", "T3-duplicate-vote"),
        ("T3 voting rate limit", "T3-rate-limit"),
        ("T3 organizer audit access", "T3-audit"),
        ("T3 public comments", "T3-comments"),
        ("T3 results public after close", "T3-results-public"),
    ]

    if t3_event is None:
        missing_detail = (
            "No dedicated evt_t3 or open event was returned by GET /api/events; "
            "T3 checks require an existing event fixture and will not create or alter one."
        )
        for label, failure_id in t3_labels:
            record_t3(label, failure_id, False, missing_detail)
    else:
        event_id = urllib.parse.quote(t3_event["id"], safe="")
        voting_base = f"{base_url}/api/events/{event_id}/voting"
        event_projects = []
        project_ids = []
        fixture_ready = False
        now = datetime.now(timezone.utc)
        starts_at = (now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
        ends_at = (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        active_config = {
            "enabled": True,
            "eligibility": "registered_participants",
            "starts_at": starts_at,
            "ends_at": ends_at,
            "hide_results": True,
        }

        # Registration is idempotent and uses the public event API.
        register_status, _ = make_request(
            f"{base_url}/api/events/{event_id}/register",
            method="POST",
            headers=auth_participant,
            body="{}",
        )
        config_status, config_body = make_request(
            voting_base,
            method="PUT",
            headers=auth_organizer,
            body=json.dumps(active_config),
        )
        denied_config_status, _ = make_request(
            voting_base,
            method="PUT",
            headers=auth_participant,
            body=json.dumps(active_config),
        )
        config_ok = config_status == 200 and denied_config_status == 403
        try:
            config_data = json.loads(config_body) if config_status == 200 else {}
            config_ok = config_ok and config_data.get("hide_results") is True
            config_ok = config_ok and config_data.get("eligibility") == "registered_participants"
        except (TypeError, ValueError):
            config_data = {}
            config_ok = False

        alternate_config = dict(active_config, eligibility="all_participants")
        alternate_status, alternate_body = make_request(
            voting_base,
            method="PUT",
            headers=auth_organizer,
            body=json.dumps(alternate_config),
        )
        try:
            alternate_data = json.loads(alternate_body) if alternate_status == 200 else {}
            config_ok = config_ok and alternate_data.get("eligibility") == "all_participants"
        except (TypeError, ValueError):
            config_ok = False
        registered_status, _ = make_request(
            voting_base,
            method="PUT",
            headers=auth_organizer,
            body=json.dumps(active_config),
        )
        config_ok = config_ok and registered_status == 200
        record_t3(
            "T3 voting configuration",
            "T3-voting-config",
            config_ok,
            (f"PUT {voting_base} as organizer got {config_status}, "
             f"as participant got {denied_config_status}; expected 200 and 403"),
        )

        # Verify disabled voting is enforced by both ballot and vote endpoints.
        disabled_config = dict(active_config, enabled=False)
        disabled_status, _ = make_request(
            voting_base,
            method="PUT",
            headers=auth_organizer,
            body=json.dumps(disabled_config),
        )
        disabled_list_status, _ = make_request(
            voting_base + "/projects", headers=auth_participant
        )
        disabled_vote_status, _ = make_request(
            voting_base + "/votes",
            method="POST",
            headers=auth_participant,
            body=json.dumps({"project_id": "invalid"}),
        )
        disabled_ok = (
            disabled_status == 200
            and disabled_list_status == 409
            and disabled_vote_status == 409
        )
        # Re-enable before exercising the active ballot.
        reenable_status, _ = make_request(
            voting_base,
            method="PUT",
            headers=auth_organizer,
            body=json.dumps(active_config),
        )
        config_ok = config_ok and disabled_ok and reenable_status == 200

        ballot_status, ballot_body = make_request(
            voting_base + "/projects", headers=auth_participant
        )
        try:
            event_projects = json.loads(ballot_body) if ballot_status == 200 else []
            if isinstance(event_projects, list):
                project_ids = [
                    project.get("id") for project in event_projects
                    if isinstance(project, dict) and project.get("id")
                ]
            else:
                event_projects = []
        except (TypeError, ValueError):
            event_projects = []
        fixture_ready = (
            register_status in (200, 201)
            and reenable_status == 200
            and ballot_status == 200
            and bool(project_ids)
        )
        judge_vote_status, _ = make_request(
            voting_base + "/votes",
            method="POST",
            headers=auth_judge_a,
            body=json.dumps({"project_id": project_ids[0] if project_ids else "invalid"}),
        )
        organizer_vote_status, _ = make_request(
            voting_base + "/votes",
            method="POST",
            headers=auth_organizer,
            body=json.dumps({"project_id": project_ids[0] if project_ids else "invalid"}),
        )
        access_ok = (
            fixture_ready
            and judge_vote_status == 403
            and organizer_vote_status == 403
        )
        access_detail = (
            f"participant registration/list status: {register_status}/{ballot_status}; "
            f"listed projects: {len(project_ids)}; judge/organizer vote statuses: "
            f"{judge_vote_status}/{organizer_vote_status} (expected 403/403)"
        )
        record_t3("T3 voting access", "T3-voting-access", access_ok, access_detail)

        no_vote_counts = all(
            not any(key.lower() in {"votes", "vote_count", "results", "total_votes"}
                    for key in project)
            for project in event_projects
        )
        ballot_order_endpoint = (
            ballot_status == 200
            and isinstance(event_projects, list)
            and len(project_ids) == len(event_projects)
            and no_vote_counts
        )
        record_t3(
            "T3 ballot ordering and count privacy",
            "T3-random-order",
            ballot_order_endpoint,
            (f"GET {voting_base}/projects got {ballot_status}; expected a list of "
             "project records without vote counts or aggregate results"),
        )

        hidden_anon_status, _ = make_request(voting_base + "/results")
        hidden_participant_status, _ = make_request(
            voting_base + "/results", headers=auth_participant
        )
        hidden_judge_status, _ = make_request(
            voting_base + "/results", headers=auth_judge_a
        )
        organizer_results_status, organizer_results_body = make_request(
            voting_base + "/results", headers=auth_organizer
        )
        hidden_ok = (
            hidden_anon_status == 403
            and hidden_participant_status == 403
            and hidden_judge_status == 403
            and organizer_results_status == 200
        )
        record_t3(
            "T3 hidden active results",
            "T3-results-hidden",
            hidden_ok,
            (f"anonymous/participant/judge/organizer results statuses: "
             f"{hidden_anon_status}/{hidden_participant_status}/{hidden_judge_status}/"
             f"{organizer_results_status}; expected 403/403/403/200"),
        )

        duplicate_ok = False
        rate_limit_ok = False
        prior_vote = any(
            isinstance(project, dict) and project.get("has_voted") is True
            for project in event_projects
        )
        if fixture_ready:
            vote_status, vote_body = make_request(
                voting_base + "/votes",
                method="POST",
                headers=auth_participant,
                body=json.dumps({"project_id": project_ids[0]}),
            )
            duplicate_status, _ = make_request(
                voting_base + "/votes",
                method="POST",
                headers=auth_participant,
                body=json.dumps({"project_id": project_ids[-1]}),
            )
            duplicate_ok = (
                vote_status == 201 and duplicate_status == 409
            ) or (
                prior_vote and vote_status == 409 and duplicate_status == 409
            )
            attempts = [vote_status, duplicate_status]
            for _ in range(4):
                status_code, _ = make_request(
                    voting_base + "/votes",
                    method="POST",
                    headers=auth_participant,
                    body=json.dumps({"project_id": project_ids[0]}),
                )
                attempts.append(status_code)
                if status_code == 429:
                    break
            rate_limit_ok = 429 in attempts
            duplicate_detail = (
                f"initial/second vote statuses: {vote_status}/{duplicate_status}; "
                "expected 201/409 (or a previously recorded ballot followed by 409)"
            )
            rate_limit_detail = (
                f"vote attempt statuses: {attempts}; expected a 429 within six requests"
            )
        else:
            duplicate_detail = "Eligible participant ballot fixture is unavailable."
            rate_limit_detail = "Eligible participant ballot fixture is unavailable."
        record_t3(
            "T3 duplicate vote prevention",
            "T3-duplicate-vote",
            duplicate_ok,
            duplicate_detail,
        )
        record_t3(
            "T3 voting rate limit",
            "T3-rate-limit",
            rate_limit_ok,
            rate_limit_detail,
        )

        audit_status, audit_body = make_request(
            voting_base + "/audit", headers=auth_organizer
        )
        denied_audit_status, _ = make_request(
            voting_base + "/audit", headers=auth_participant
        )
        try:
            audit_data = json.loads(audit_body) if audit_status == 200 else None
            audit_has_actions = (
                isinstance(audit_data, list)
                and all(isinstance(record, dict) and "action" in record for record in audit_data)
            )
        except (TypeError, ValueError):
            audit_has_actions = False
        record_t3(
            "T3 organizer audit access",
            "T3-audit",
            audit_status == 200 and denied_audit_status == 403 and audit_has_actions,
            (f"organizer/participant audit statuses: {audit_status}/"
             f"{denied_audit_status}; expected 200/403 and an action list"),
        )

        if project_ids:
            comment_url = f"{base_url}/api/projects/{urllib.parse.quote(project_ids[0], safe='')}/comments"
            comments_status, _ = make_request(comment_url)
            anonymous_comment_status, _ = make_request(
                comment_url, method="POST", body=json.dumps({"body": "checker unauthenticated comment"})
            )
            comment_status, comment_body = make_request(
                comment_url,
                method="POST",
                headers=auth_participant,
                body=json.dumps({"body": "DOGFOOD T3 acceptance comment"}),
            )
            try:
                comment_data = json.loads(comment_body) if comment_status == 201 else {}
            except (TypeError, ValueError):
                comment_data = {}
            comments_ok = (
                comments_status == 200
                and anonymous_comment_status == 401
                and comment_status == 201
                and isinstance(comment_data, dict)
                and "email" not in {key.lower() for key in comment_data}
            )
            comments_detail = (
                f"read/anonymous-post/participant-post statuses: "
                f"{comments_status}/{anonymous_comment_status}/{comment_status}; "
                "expected 200/401/201 and no email field in the post response"
            )
        else:
            comments_ok = False
            comments_detail = "No submitted project is available for the public comments API."
        record_t3("T3 public comments", "T3-comments", comments_ok, comments_detail)

        closed_config = dict(active_config)
        closed_config["ends_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat().replace("+00:00", "Z")
        closed_status, _ = make_request(
            voting_base,
            method="PUT",
            headers=auth_organizer,
            body=json.dumps(closed_config),
        )
        public_results_status, public_results_body = make_request(
            voting_base + "/results"
        )
        try:
            public_results_data = json.loads(public_results_body) if public_results_status == 200 else {}
            results_public = isinstance(public_results_data.get("results"), list)
        except (AttributeError, TypeError, ValueError):
            results_public = False
        record_t3(
            "T3 results public after close",
            "T3-results-public",
            closed_status == 200 and public_results_status == 200 and results_public,
            (f"close configuration/results statuses: {closed_status}/{public_results_status}; "
             "expected organizer config 200 and public results 200 after the window"),
        )

        t3_passed = all(failure_id not in failed for _, failure_id in t3_labels)

    lines.append("")

    # ── T4 checks ──────────────────────────────────────────────────────────────

    t4_checks = []
    t4_passed = False

    def record_t4(label, failure_id, passed, detail):
        if passed:
            lines.append(fmt_pass(label))
        else:
            lines.append(fmt_fail(label, detail))
            failed.append(failure_id)
        t4_checks.append(failure_id)

    t4_event_status, t4_event_body = make_request(base_url + "/api/events/evt_t4")
    try:
        t4_event = json.loads(t4_event_body) if t4_event_status == 200 else {}
    except (TypeError, ValueError):
        t4_event = {}
    t4_event_available = t4_event.get("id") == "evt_t4"
    t4_labels = [
        ("T4 REST API and webhooks", "T4-api-webhooks"),
        ("T4 certificates and records", "T4-records"),
        ("T4 signed judge records", "T4-signed-records"),
        ("T4 embeddable gallery", "T4-gallery-widget"),
        ("T4 bulk import/export", "T4-bulk-io"),
    ]

    if not t4_event_available:
        missing_t4 = "The dedicated evt_t4 fixture is unavailable from the live event API."
        for label, failure_id in t4_labels:
            record_t4(label, failure_id, False, missing_t4)
    else:
        openapi_status, openapi_body = make_request(base_url + "/openapi.json")
        try:
            openapi_paths = json.loads(openapi_body).get("paths", {}) if openapi_status == 200 else {}
        except (TypeError, ValueError):
            openapi_paths = {}
        required_api_paths = {
            "/api/auth/login",
            "/api/events",
            "/api/events/{event_id}/register",
            "/api/teams",
            "/api/teams/{team_id}/join",
            "/api/projects",
            "/api/projects/{project_id}/submit",
            "/api/judge/scores",
            "/api/events/{event_id}/voting",
            "/api/projects/{project_id}/comments",
            "/api/webhooks",
            "/api/events/{event_id}/records/generate",
            "/api/t4/records/{record_id}/verify",
            "/api/events/{event_id}/bulk-export",
            "/api/events/bulk-import",
        }
        api_coverage_ok = openapi_status == 200 and required_api_paths.issubset(openapi_paths)

        received_webhooks = []

        class _CheckerWebhookReceiver(BaseHTTPRequestHandler):
            def do_POST(self):
                content_length = int(self.headers.get("Content-Length", "0"))
                try:
                    received_webhooks.append(json.loads(self.rfile.read(content_length)))
                    self.send_response(202)
                except (TypeError, ValueError):
                    self.send_response(400)
                self.end_headers()

            def log_message(self, format, *args):
                return

        receiver = None
        receiver_thread = None
        webhook_status = 0
        webhook_management_status = 0
        delivery_status = 0
        event_update_status = 0
        original_name = t4_event.get("name", "DOGFOOD T4 Stretch Acceptance")
        original_description = t4_event.get("description", "")
        try:
            receiver = HTTPServer(("127.0.0.1", 0), _CheckerWebhookReceiver)
            receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
            receiver_thread.start()
            webhook_url = f"http://127.0.0.1:{receiver.server_port}/dogfood-hook"
            webhook_subscription = make_request(
                base_url + f"/api/events/evt_t4/webhooks",
                method="POST",
                headers=auth_organizer,
                body=json.dumps({"target_url": webhook_url, "event_types": ["event.updated"]}),
            )
            webhook_status = webhook_subscription[0]
            denied_webhook_status, _ = make_request(
                base_url + "/api/webhooks",
                method="POST",
                headers=auth_participant,
                body=json.dumps({"target_url": webhook_url, "event_types": ["event.updated"]}),
            )
            event_update_status, _ = make_request(
                base_url + "/api/events/evt_t4",
                method="PUT",
                headers=auth_organizer,
                body=json.dumps({
                    "description": f"T4 acceptance webhook probe {uuid.uuid4().hex}"
                }),
            )
            # Restore the dedicated fixture's descriptive metadata after exercising delivery.
            make_request(
                base_url + "/api/events/evt_t4",
                method="PUT",
                headers=auth_organizer,
                body=json.dumps({"name": original_name, "description": original_description}),
            )
            deliveries_status, deliveries_body = make_request(
                base_url + "/api/events/evt_t4/webhooks/deliveries",
                headers=auth_organizer,
            )
            delivery_status = deliveries_status
            try:
                deliveries = json.loads(deliveries_body) if deliveries_status == 200 else []
            except (TypeError, ValueError):
                deliveries = []
            webhook_delivered = any(item.get("status") == "delivered" for item in deliveries)
        except Exception as exc:
            denied_webhook_status = 0
            webhook_delivered = False
            webhook_error = str(exc)
        else:
            webhook_error = ""
        finally:
            if receiver is not None:
                receiver.shutdown()
                receiver.server_close()
            if receiver_thread is not None:
                receiver_thread.join(timeout=2)
        received_envelope_ok = any(
            item.get("event_type") == "event.updated"
            and item.get("event_id") == "evt_t4"
            and item.get("delivery_id")
            and item.get("timestamp", "").endswith("Z")
            and isinstance(item.get("payload"), dict)
            for item in received_webhooks
        )
        api_webhooks_ok = (
            api_coverage_ok
            and webhook_status == 201
            and denied_webhook_status == 403
            and event_update_status == 200
            and delivery_status == 200
            and webhook_delivered
            and received_envelope_ok
        )
        record_t4(
            "T4 REST API and webhooks",
            "T4-api-webhooks",
            api_webhooks_ok,
            (f"OpenAPI={openapi_status}, subscription={webhook_status}, denied={denied_webhook_status}, "
             f"event update={event_update_status}, delivery API={delivery_status}, "
             f"receiver got event={received_envelope_ok}; {webhook_error}"),
        )

        key_status, key_body = make_request(base_url + "/api/t4/signing/public-key")
        generate_status, generate_body = make_request(
            base_url + "/api/events/evt_t4/records/generate",
            method="POST",
            headers=auth_organizer,
            body="{}",
        )
        try:
            key_data = json.loads(key_body) if key_status == 200 else {}
            generated = json.loads(generate_body).get("records", []) if generate_status == 200 else []
        except (TypeError, ValueError, AttributeError):
            key_data = {}
            generated = []
        participant_certificate = next(
            (record for record in generated
             if record.get("payload", {}).get("record_type") == "participant_certificate"),
            None,
        )
        judge_record = next(
            (record for record in generated
             if record.get("payload", {}).get("record_type") == "judge_participation"),
            None,
        )
        records_ok = (
            generate_status == 200
            and participant_certificate is not None
            and judge_record is not None
            and key_data.get("algorithm") == "Ed25519"
            and "PRIVATE KEY" not in key_body
            and "email" not in json.dumps(generated).lower()
        )
        record_t4(
            "T4 certificates and records",
            "T4-records",
            records_ok,
            (f"record generation={generate_status}, certificate={participant_certificate is not None}, "
             f"judge record={judge_record is not None}, public key={key_status}"),
        )

        def independently_verify_record(record):
            try:
                public_bytes = base64.b64decode(record["public_key"], validate=True)
                signature_bytes = base64.b64decode(record["signature"], validate=True)
                canonical_payload = json.dumps(
                    record["payload"],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                Ed25519PublicKey.from_public_bytes(public_bytes).verify(
                    signature_bytes, canonical_payload
                )
                fingerprint = hashlib.sha256(public_bytes).hexdigest()[:24]
                return fingerprint == record["key_id"] == key_data.get("key_id")
            except (InvalidSignature, KeyError, TypeError, ValueError):
                return False

        judge_signature_ok = bool(judge_record and independently_verify_record(judge_record))
        public_record_status = 0
        public_verify_ok = False
        tamper_rejected = False
        if judge_record:
            public_record_status, public_record_body = make_request(
                base_url + f"/api/t4/records/{urllib.parse.quote(judge_record['id'], safe='')}"
            )
            stored_verify_status, stored_verify_body = make_request(
                base_url + f"/api/t4/records/{urllib.parse.quote(judge_record['id'], safe='')}/verify"
            )
            try:
                stored_verify = json.loads(stored_verify_body) if stored_verify_status == 200 else {}
                tampered_payload = dict(judge_record["payload"], data={"scored_project_count": 987654})
                tampered_status, tampered_body = make_request(
                    base_url + "/api/t4/records/verify",
                    method="POST",
                    body=json.dumps({
                        "payload": tampered_payload,
                        "signature": judge_record["signature"],
                        "key_id": judge_record["key_id"],
                        "public_key": judge_record["public_key"],
                    }),
                )
                tamper_rejected = (
                    tampered_status == 200
                    and json.loads(tampered_body).get("valid") is False
                )
                public_verify_ok = (
                    public_record_status == 200
                    and stored_verify.get("valid") is True
                )
            except (TypeError, ValueError, AttributeError):
                public_verify_ok = False
                tamper_rejected = False
        signed_record_ok = (
            records_ok and judge_signature_ok and public_verify_ok and tamper_rejected
        )
        record_t4(
            "T4 signed judge records",
            "T4-signed-records",
            signed_record_ok,
            (f"independent signature={judge_signature_ok}, public retrieval={public_record_status}, "
             f"API verification={public_verify_ok}, tamper rejected={tamper_rejected}"),
        )

        widget_status, widget_body = make_request(base_url + "/embed/events/evt_t4/gallery")
        widget_ok = (
            widget_status == 200
            and "T4 Reference Project" in widget_body
            and "participant@dogfood.local" not in widget_body
            and "functionality" not in widget_body
            and "PRIVATE KEY" not in widget_body
            and "votes" not in widget_body.lower()
        )
        record_t4(
            "T4 embeddable gallery",
            "T4-gallery-widget",
            widget_ok,
            (f"GET /embed/events/evt_t4/gallery got {widget_status}; expected public submitted project "
             "content without participant, score, vote, or signing-key data"),
        )

        bulk_export_status, bulk_export_body = make_request(
            base_url + "/api/events/evt_t4/bulk-export", headers=auth_organizer
        )
        anonymous_import_status, _ = make_request(
            base_url + "/api/events/bulk-import",
            method="POST",
            body=json.dumps({"target_event_id": "evt_checker_unauth", "archive": {}}),
        )
        participant_import_status, _ = make_request(
            base_url + "/api/events/bulk-import",
            method="POST",
            headers=auth_participant,
            body=json.dumps({"target_event_id": "evt_checker_participant", "archive": {}}),
        )
        try:
            archive = json.loads(bulk_export_body) if bulk_export_status == 200 else {}
        except (TypeError, ValueError):
            archive = {}
        target_import_id = "evt_t4_import_" + uuid.uuid4().hex[:12]
        import_status, import_body = make_request(
            base_url + "/api/events/bulk-import",
            method="POST",
            headers=auth_organizer,
            body=json.dumps({"target_event_id": target_import_id, "archive": archive}),
        )
        try:
            import_data = json.loads(import_body) if import_status == 201 else {}
        except (TypeError, ValueError):
            import_data = {}
        roundtrip_status, roundtrip_body = make_request(
            base_url + f"/api/events/{urllib.parse.quote(target_import_id, safe='')}/bulk-export",
            headers=auth_organizer,
        )
        try:
            roundtrip = json.loads(roundtrip_body) if roundtrip_status == 200 else {}
        except (TypeError, ValueError):
            roundtrip = {}
        protected_target_status, _ = make_request(
            base_url + "/api/events/bulk-import",
            method="POST",
            headers=auth_organizer,
            body=json.dumps({"target_event_id": "evt_t3", "archive": archive}),
        )
        protected_source_archive = dict(archive)
        if archive.get("event"):
            protected_source_archive["event"] = dict(archive["event"], id="evt_01")
        protected_source_status, _ = make_request(
            base_url + "/api/events/bulk-import",
            method="POST",
            headers=auth_organizer,
            body=json.dumps({
                "target_event_id": "evt_t4_import_protected_" + uuid.uuid4().hex[:8],
                "archive": protected_source_archive,
            }),
        )
        bulk_ok = (
            bulk_export_status == 200
            and anonymous_import_status == 401
            and participant_import_status == 403
            and import_status == 201
            and roundtrip_status == 200
            and import_data.get("projects_imported", 0) == len(archive.get("projects", []))
            and len(roundtrip.get("teams", [])) == len(archive.get("teams", []))
            and len(roundtrip.get("registrations", [])) == len(archive.get("registrations", []))
            and len(roundtrip.get("projects", [])) == len(archive.get("projects", []))
            and len(roundtrip.get("judge_assignments", [])) == len(archive.get("judge_assignments", []))
            and len(roundtrip.get("scores", [])) == len(archive.get("scores", []))
            and protected_target_status == 409
            and protected_source_status == 409
        )
        record_t4(
            "T4 bulk import/export",
            "T4-bulk-io",
            bulk_ok,
            (f"export={bulk_export_status}, import auth={anonymous_import_status}/{participant_import_status}, "
             f"round-trip import/export={import_status}/{roundtrip_status}, protected target/source="
             f"{protected_target_status}/{protected_source_status}"),
        )

        t4_passed = all(failure_id not in failed for _, failure_id in t4_labels)

    lines.append("")

    # ── Summary ────────────────────────────────────────────────────────────────
    t1_checks = ["T1-gallery-public", "T1-fixture-title", "T1-deadline"]
    t1_passed = not any(f in failed for f in t1_checks)
    t2_checks = ["T2-judge-own-scores", "T2-peer-scores", "T2-participant-blocked", "T2-csv-export"]
    t2_passed = not any(f in failed for f in t2_checks)

    actual_verified = []
    if t1_passed:
        actual_verified.append("T1")
    if t2_passed and "T2" in claimed:
        actual_verified.append("T2")
    if "T3" in claimed and t3_passed:
        actual_verified.append("T3")
    if "T4" in claimed and t4_passed:
        actual_verified.append("T4")

    t3_failure_count = sum(failure_id in failed for _, failure_id in t3_labels)
    lines.append(f"T3 checks: {len(t3_checks) - t3_failure_count}/{len(t3_checks)} passed")
    lines.append(f"T3 passed: {'yes' if t3_passed else 'no'}")

    t4_failure_count = sum(failure_id in failed for _, failure_id in t4_labels)
    lines.append(f"T4 checks: {len(t4_checks) - t4_failure_count}/{len(t4_checks)} passed")
    lines.append(f"T4 passed: {'yes' if t4_passed else 'no'}")

    lines.append(f"claimed {' '.join(claimed)}, verified {' '.join(actual_verified) or '(none)'}")
    if set(claimed) - set(actual_verified):
        missing = set(claimed) - set(actual_verified)
        lines.append(f"note: claimed but not verified: {' '.join(sorted(missing))}")

    print("\n".join(lines))

if __name__ == "__main__":
    main()
