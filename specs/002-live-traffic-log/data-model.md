# Data Model: Live Traffic Log

## In-memory

### Event (transient)
| field | type | note |
|---|---|---|
| at | datetime UTC | panel receipt time |
| user_id | int \| None | parsed from `email:` when numeric |
| user_label | str | raw `email:` token when not numeric |
| node_id | int | |
| inbound | str | inbound tag (may contain spaces) |
| host | str | destination name or address (IPv6 without brackets) |
| port | int | |
| protocol | "tcp" \| "udp" | |
| route | str | outbound tag |
| refused | bool | `route == "BLOCK"` |

### Bucket key → accumulator
key = (user_id, node_id, inbound, host, port, protocol, refused, bucket_start); accumulator = {first_seen, last_seen, hits, route, row_id | None}. Cap 50,000 keys per flush window.

### Node collection status
| field | type |
|---|---|
| node_id | int |
| state | "collecting" \| "attaching" \| "error" \| "detached" \| "paused" \| "unavailable" \| "no_reports" |
| since | datetime |
| lines | int (raw lines seen) |
| events | int (parsed destination reports) |
| dropped | int (tap/live/bucket-cap drops) |
| records | int (rows written) |
| last_event_at | datetime \| None |
| detail | str (error text or reason) |

## Tables (Alembic revision `a7d5e1f3b294`, down_revision `f6c4d0e2a183`)

### traffic_log_records
| column | type | constraints |
|---|---|---|
| id | BigInteger (SqliteCompatibleBigInteger) | PK autoincrement |
| bucket_start | DateTime(tz) | NOT NULL, index |
| user_id | BigInteger | NULL allowed (unresolved label), index with last_seen |
| user_label | String(128) | NULL; raw token when user_id is NULL |
| node_id | BigInteger | NOT NULL |
| inbound_tag | String(256) | NOT NULL |
| host | String(255) | NOT NULL |
| port | Integer | NOT NULL |
| protocol | String(3) | NOT NULL |
| refused | Boolean | NOT NULL default false |
| route | String(128) | NOT NULL |
| first_seen | DateTime(tz) | NOT NULL |
| last_seen | DateTime(tz) | NOT NULL |
| hits | Integer | NOT NULL default 1 |

Indexes: `ix_traffic_log_records_bucket_start (bucket_start)`, `ix_traffic_log_records_user_last (user_id, last_seen)`, `ix_traffic_log_records_last_id (last_seen, id)`, `ix_traffic_log_records_node_last (node_id, last_seen)`.
No foreign keys: rows outlive users and nodes; retention and the ceiling remove them.

### traffic_log_identities
| column | type | constraints |
|---|---|---|
| user_id | BigInteger | PK |
| username | String(128) | NOT NULL |
| admin_id | BigInteger | NULL |
| deleted | Boolean | NOT NULL default false |
| updated_at | DateTime(tz) | NOT NULL |

Refreshed whenever a user id is seen and the cache entry is older than 60 s; `deleted` set when the id no longer exists in `users`.

### traffic_log_state
| column | type | constraints |
|---|---|---|
| id | Integer | PK, always 1 |
| enabled | Boolean | NOT NULL default true |
| updated_at | DateTime(tz) | NOT NULL |

## Validation rules
- History `start`/`end`: both required, `end > start`, `start ≥ now − 48 h`, else HTTP 422 with the two-day message (no clamping).
- `username`: exact match, resolved through `users` then `traffic_log_identities`; unknown → empty result (200) not 404.
- `limit`: 1–200, default 100. `cursor`: opaque `<last_seen_iso>|<id>`.
- Non-sudo admins: every query joins `traffic_log_identities` and requires `admin_id = current admin`; the live feed applies the same predicate per event (via the identity cache) before enqueueing.

## State transitions (per node)
`detached → attaching → collecting → (error | detached)`; `paused` overrides all while `enabled = false`; `unavailable` is terminal for the process when `workers > 1`; `no_reports` is `collecting` with `events = 0` for ≥ 60 s after `lines > 0`.

## Retention and ceiling
Purge job (`traffic_log_purge`, interval 600 s): (1) delete `bucket_start < now − 48 h` in 5,000-id chunks, ≤ 200,000 per run; (2) if `max(id) − ceiling > 0`, delete `id < max(id) − ceiling` in the same chunks. Result counters are exposed in `/status` (`last_purge_at`, `purged_expired`, `purged_over_ceiling`, `ceiling_active`).
