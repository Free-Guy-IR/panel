# Data Model: Close Open Audit Items (v5.34.26)

## New table: named_locks (fork, P2)
| Column | Type | Rules |
|---|---|---|
| name | VARCHAR(64) | primary key |
| acquired_at | DATETIME, nullable | set to now on every acquisition; informational only |

- Seed row: `('proxy_secrets', NULL)`, inserted by the migration.
- Migration: `down_revision = d8b2c4f60a17`. Upgrade creates the table and inserts the seed row; downgrade drops the table.
- Lifecycle: the row is never deleted by the application. A missing row is re-seeded on first use by an insert-ignore; a missing table makes the guarded write fail.
- The users table is not changed.

## Changed behaviour on existing entities (no schema change)
- **User.status**: active/on_hold → limited in the usage tick when `is_limited` becomes true (A9). A user with a next plan goes through `reset_user_by_next` instead.
- **User.proxy_settings.wireguard**: for users without WireGuard access, never written from client input (E1). For WireGuard-bound users processed by reconcile, a shared key is regenerated (E1).
- **User.proxy_settings.mtproto.secret**: unique across users. A colliding value (supplied or generated) is replaced by a fresh unique value; 409 only after 5 attempts (E4b).
- **User.proxy_settings.l2tp.password**: bulk activation uses the per-key conditional update (E4b).
- **Admin.used_traffic on reset**: decremented by the logged value instead of set to 0.
- **node_user_usages (chart)**: written in the same transaction as `users.used_traffic` for a cohort (A2-r). Fenced users' rows are never written.
- **Guarded secret set**: hysteria.auth, hysteria2.password, tuic.password, tuic.uuid, trojan.password, shadowsocks.password, vless.id, vmess.id, plus mtproto.secret.
