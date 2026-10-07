# Behaviour Contracts (v5.34.26)

No new route. The existing routes change behaviour as follows.

| Route / trigger | Before (v5.34.24) | After (v5.34.26) |
|---|---|---|
| POST /api/users/bulk/l2tp_activate (users:update) | Whole-document replace, no uniqueness check | Same request/response shape as mtproto_activate/openvpn_activate (dry_run → missing count, else granted count); guarded per-key update; unique passwords |
| POST /api/user, PUT /api/user/{username} (and /by-username, /by-id) with `proxy_settings.mtproto.secret` held by another user | Stored as a duplicate | Replaced by a fresh unique secret (WARNING logged), 200; 409 only after 5 failed regenerations — the rule the 8 guarded secrets already follow |
| POST /api/user, PUT /api/user/{username} (and /by-username, /by-id) with a WireGuard keypair for a user without WireGuard access | Stored | Ignored; the stored value (or empty) is kept; 200 |
| User create/modify/bulk-create or bulk activation that cannot take the secret lock within the DB lock timeout | 500 or duplicate risk | 503, nothing stored (other callers keep their existing error mapping) |
| GET links (subscription `links` format and `/api/user/{id}/subscription/links`) for a user whose only links are MTProto without a secret | 200 with empty body | 409 "MTProto has not been activated for this user yet; ask the administrator to activate it" |
| PUT /api/node/{id} changing only name and/or usage_coefficient | Core stopped and reconnected | No stop; the live connection keeps running with the new values |
| POST /api/admin/{username}/reset, /by-username/{username}/reset, /by-id/{admin_id}/reset and the bulk admin reset | used_traffic := 0 | used_traffic := used_traffic − logged value |
| Usage tick | Limits users only in the review job | Limits billed users in the same tick (review job still runs) |
| POST /api/traffic-log/purge | unchanged | unchanged (now pinned by an HTTP test: owner 200 + rows removed; non-owner 403) |
| Dashboard all-nodes chart | Header total ≠ drawn sum when deleted-node usage exists | "Deleted nodes" series in bars/area/legend/pie; header = drawn sum |
