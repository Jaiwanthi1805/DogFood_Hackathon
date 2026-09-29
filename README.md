# DOGFOOD 2026 Portal

A self-hosted hackathon submission and judging portal for the DOGFOOD 2026 competition.

## Quick Start

```bash
docker compose up
```

The portal starts at **http://localhost:8080**

The seed script runs automatically on startup. It loads `fixtures.json` and prints auth tokens.

## Demo Accounts

| Role        | Email                      | Password        |
|-------------|---------------------------|-----------------|
| Organizer   | organizer@dogfood.local    | organizer123    |
| Participant | participant@dogfood.local  | participant123  |
| Judge A     | judge_a@dogfood.local      | judge_a123      |
| Judge B     | judge_b@dogfood.local      | judge_b123      |

## Authentication

The application uses **Bearer token authentication**.

Log in at `/api/auth/login` with email/password to receive a token.

Use the token as: `Authorization: Bearer <token>`

Seed tokens are deterministic and printed on startup.

## Key Routes

| Route | Description |
|-------|-------------|
| `GET /projects` | Public gallery (no auth required) |
| `POST /api/projects/submit-new` | Submit a project (participant only, deadline enforced) |
| `GET /openapi.json` | FastAPI OpenAPI document |
| `POST /api/auth/login` | Login |
| `GET /api/events` | List events |
| `POST /api/events` | Create event (organizer only) |
| `PUT /api/events/{id}/voting` | Configure community voting (organizer only) |
| `GET /api/events/{id}/vote` | Community voting page |
| `POST /api/events/{id}/voting/votes` | Cast one community vote |
| `GET /api/projects/{id}/comments` | Read project comments |
| `POST /api/projects/{id}/comments` | Add a participant comment |
| `POST /api/events/{id}/records/generate` | Issue event certificates and signed judge records |
| `GET /api/t4/records/{id}/verify` | Public signed-record verification |
| `GET /embed/events/{id}/gallery` | Public iframe gallery |
| `GET /api/events/{id}/bulk-export` | Organizer event archive export |
| `POST /api/events/bulk-import` | Organizer event archive import |
| `GET /api/teams` | List teams |
| `POST /api/teams` | Create team (participant only) |

## Tech Stack

- **Backend:** Python 3.12 + FastAPI
- **Database:** SQLite (via SQLAlchemy ORM)
- **Templates:** Jinja2
- **Container:** Docker + Docker Compose

## Running Tests

```bash
cd src
pip install -r requirements.txt
pip install pytest httpx
cd ..
pytest tests/
```

## T1 Features

- ✅ Local authentication (email/password → Bearer token)
- ✅ Three roles: Organizer, Participant, Judge
- ✅ Role enforcement in backend (not just frontend)
- ✅ Event creation (organizer only) with tracks
- ✅ Event registration for participants
- ✅ Team creation and joining
- ✅ Project creation with draft/submitted states
- ✅ **Backend deadline enforcement** — POST to closed event returns 422
- ✅ Public project gallery at `/projects` (HTTP 200, no auth)
- ✅ Official fixtures.json loaded on startup (40 projects visible in gallery)
- ✅ `docker compose up` starts a fully working, seeded portal

## T3 Features

- Event-level voting window, hidden-results setting, and registered/all-participant eligibility
- One vote per eligible participant per event, with backend rate limiting and audit events
- Randomized project order and organizer-only active results when configured
- Public comment reading and participant-only validated comment posting

## T4 Features

- Ed25519-signed participant certificates and judge participation records with public verification
- Organizer-managed event webhooks with persisted delivery outcomes
- Public submitted-project iframe gallery
- Versioned organizer-only event archive import/export

See [T4.md](T4.md) for record, webhook, embed, and archive formats.

## Honest Limitations

- Open-link/email-gated community voting is not implemented; supported eligibility modes require participant accounts.
- SQLite is not production-grade for concurrent heavy loads (fine for hackathon)
