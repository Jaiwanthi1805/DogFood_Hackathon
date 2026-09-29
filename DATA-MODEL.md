# Data Model

## Entity-Relationship Overview

```
users ──< auth_tokens
users ──< team_members >── teams
users ──< event_registrations >── events
events ──< tracks
teams >── events
projects >── teams
projects >── tracks
projects >── events
scores >── projects
events ── event_voting_configs
events ──< community_votes >── projects
users ──< community_votes
projects ──< project_comments >── users
events ──< voting_rate_limit_hits
events ──< voting_audit_records
```

## Tables

### users
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | UUID or seed ID |
| email | String UNIQUE | Login credential |
| name | String | Display name |
| hashed_password | String | SHA-256 of password |
| role | String | `organizer` \| `participant` \| `judge` |
| is_seeded | Boolean | True for fixture-derived rows |

### auth_tokens
| Column | Type | Notes |
|--------|------|-------|
| token | String PK | Random hex 32 bytes |
| user_id | String FK | → users.id |
| created_at | DateTime | UTC |

### events
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | `evt_01` from fixture |
| name | String | Event name |
| description | Text | Optional |
| submissions_close | DateTime | **Official fixture timestamp used as-is** |
| created_by | String FK | → users.id |
| is_seeded | Boolean | |

### tracks
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | `trk_01` … `trk_08` |
| event_id | String FK | → events.id |
| name | String | Track name |

### event_registrations
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | UUID |
| event_id | String FK | → events.id |
| user_id | String FK | → users.id |
| registered_at | DateTime | |

### teams
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | `tm_01` … `tm_40` |
| name | String | Team name |
| event_id | String FK | → events.id |
| is_seeded | Boolean | |

### team_members
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | UUID |
| team_id | String FK | → teams.id |
| user_id | String FK | → users.id (nullable for fixture email-only members) |
| email | String | Member email |

### projects
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | `prj_01` … `prj_41` |
| team_id | String FK | → teams.id |
| track_id | String FK | → tracks.id |
| event_id | String FK | → events.id |
| owner_id | String FK | → users.id |
| title | String | Project title |
| summary | Text | Short description |
| repo_url | String | Repository URL |
| status | String | `draft` \| `submitted` |
| submitted_at | DateTime | Null for drafts |
| is_seeded | Boolean | |

### judge_tracks
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | UUID |
| judge_id | String | Fixture judge ID (e.g. `jdg_01`) |
| track_id | String FK | → tracks.id |
| user_id | String FK | → users.id (linked seed account if applicable) |

### scores
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | UUID |
| judge_fixture_id | String | Fixture judge ID |
| judge_user_id | String FK | → users.id (nullable) |
| project_id | String FK | → projects.id |
| criteria | JSON | `{"functionality": 4, "quality": 3, "innovation": 2}` |
| comment | Text | Judge comment |

### event_voting_configs
| Column | Type | Notes |
|--------|------|-------|
| event_id | String PK/FK | → events.id |
| enabled | Boolean | Organizer-controlled voting switch |
| eligibility | String | `registered_participants` or `all_participants` |
| starts_at / ends_at | DateTime | UTC voting window |
| hide_results | Boolean | Hide aggregates from non-organizers during active voting |

### community_votes
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | UUID |
| event_id | String FK | → events.id |
| project_id | String FK | → projects.id |
| voter_id | String FK | → users.id |
| created_at | DateTime | UTC; unique `(event_id, voter_id)` constraint |

### project_comments
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | UUID |
| project_id | String FK | → projects.id |
| author_id | String FK | → users.id |
| body | Text | Trimmed, 1-1000 characters |
| created_at | DateTime | UTC |

### voting_rate_limit_hits
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | UUID |
| event_id / voter_id | String FK | Per-account, per-event throttle key |
| created_at | DateTime | UTC; persisted sliding-window hit |

### voting_audit_records
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | UUID |
| event_id | String FK | → events.id |
| actor_id | String FK | → users.id (nullable) |
| action | String | Configuration, vote, duplicate, and rate-limit actions |
| project_id | String FK | Optional affected project |
| created_at | DateTime | UTC; no email, IP, or ballot payload |

New T3 data uses separate tables. Startup `Base.metadata.create_all()` creates these tables for existing SQLite databases without altering seeded T1/T2 event, project, or score rows.

## T4 Tables

### issued_records
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | Stable `rec_` identifier derived from event, record type, and subject |
| event_id / subject_id | String FK | Event and user represented by the record |
| record_type | String | `participant_certificate` or `judge_participation` |
| payload_json | JSON | Versioned public record payload; contains no email or private scores |
| signature | Text | Base64 Ed25519 signature over canonical payload JSON |
| public_key / key_id | Text / String | Base64 raw public key and SHA-256 fingerprint |
| created_at | DateTime | UTC record persistence time |

### webhook_subscriptions
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | UUID |
| event_id | String FK | Nullable for global subscriptions; otherwise scoped to one event |
| target_url | String | Validated HTTP(S), public or loopback destination |
| event_types | JSON | Subscribed T4 event names |
| created_by | String FK | Organizer who registered the destination |
| active | Boolean | Soft-disable flag |

### webhook_deliveries
| Column | Type | Notes |
|--------|------|-------|
| id | String PK | Stable hash-based delivery ID |
| subscription_id / event_id | String FK | Subscription and event that produced the delivery |
| event_type | String | Delivered event name |
| payload_json | JSON | Versioned event envelope, sanitized of credentials and private fields |
| status / response_status | String / Integer | Delivery outcome and optional HTTP response |
| attempts / error | Integer / Text | Attempt count and bounded failure detail |
| created_at / delivered_at | DateTime | UTC delivery timestamps |

T4 tables are additive and are created by the existing startup `Base.metadata.create_all()` call. The private Ed25519 key is stored separately at `data/t4_ed25519_private.pem` (or `DOGFOOD_SIGNING_KEY_PATH`) and is excluded from version control.

## Fixture Mapping

The official `fixtures.json` maps to our schema as follows:

```
fixtures.event          → events row (id, name, submissions_close)
fixtures.tracks[]       → tracks rows (id, name, event_id=evt_01)
fixtures.judges[]       → judge_tracks rows + linked User rows
fixtures.teams[]        → teams rows + team_members rows
fixtures.teams[].members[] → users rows (role=participant, is_seeded=true)
fixtures.projects[]     → projects rows (status=submitted)
fixtures.scores[]       → scores rows (preserved for T2)
```

## Deadline Enforcement

The `submissions_close` from fixtures (`2026-03-01T18:00:00Z`) is stored **as-is** in the database. No substitution is made.

The backend compares `datetime.utcnow()` against `event.submissions_close` on every submission attempt at `POST /api/projects/submit-new` and `POST /api/projects/{id}/submit`. If the current time is >= close time, HTTP 422 is returned.
