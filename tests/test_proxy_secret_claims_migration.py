import json
import logging
import sqlite3
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config

from app.db import base
from app.fork.proxy_secrets.claims import secret_digest
from app.fork.proxy_secrets.fields import ProxySecretField
from app.models.proxy import ProxyTable

BEFORE_CLAIMS = "fd192e9628a5"
CLAIMS = "e79833e2a0b2"

pytestmark = pytest.mark.skipif(not base.database_settings.is_sqlite, reason="the migration fixture builds a SQLite file")


def _config(path) -> Config:
    config = Config()
    config.set_main_option("script_location", "app/db/migrations")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


def _run(action, config, revision):
    states = {
        logger: logger.disabled
        for logger in logging.root.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    }
    try:
        action(config, revision)
    finally:
        for logger, disabled in states.items():
            logger.disabled = disabled


def _route_twin(value: str) -> str:
    raw = bytearray(UUID(value).bytes)
    raw[6] ^= 0x11
    return str(UUID(bytes=bytes(raw)))


def _seed(path, users: dict[str, dict]) -> dict[str, int]:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    connection = sqlite3.connect(path)
    try:
        role_id = connection.execute("SELECT id FROM admin_roles ORDER BY id LIMIT 1").fetchone()[0]
        connection.execute(
            "INSERT INTO admins (username, hashed_password, created_at, role_id) VALUES ('claims-owner', 'x', ?, ?)",
            (now, role_id),
        )
        admin_id = connection.execute("SELECT id FROM admins WHERE username = 'claims-owner'").fetchone()[0]
        ids = {}
        for username, settings in users.items():
            cursor = connection.execute(
                "INSERT INTO users (username, status, used_traffic, admin_id, proxy_settings, created_at) "
                "VALUES (?, 'active', 0, ?, ?, ?)",
                (username, admin_id, json.dumps(settings), now),
            )
            ids[username] = cursor.lastrowid
        connection.commit()
        return ids
    finally:
        connection.close()


def _settings(path) -> dict[int, dict]:
    connection = sqlite3.connect(path)
    try:
        return {user_id: json.loads(raw) for user_id, raw in connection.execute("SELECT id, proxy_settings FROM users")}
    finally:
        connection.close()


def _claims(path) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT kind, digest, user_id, retired_at FROM proxy_secret_claims").fetchall()
    finally:
        connection.close()


def _user(**sections) -> dict:
    settings = ProxyTable().dict()
    for protocol, section in sections.items():
        if section is None:
            settings.pop(protocol, None)
        else:
            settings[protocol] = section
    return settings


def test_the_backfill_rotates_every_member_of_a_shared_group_and_nobody_else(tmp_path):
    path = tmp_path / "claims.db"
    config = _config(path)
    _run(upgrade, config, BEFORE_CLAIMS)
    shared_auth = f"shared-{uuid4().hex}"
    shared_password = f"shared-{uuid4().hex}"
    vless = str(uuid4())
    ids = _seed(
        path,
        {
            "a": _user(hysteria={"auth": shared_auth}),
            "b": _user(hysteria={"auth": shared_auth}, hysteria2={"password": shared_password}),
            "c": _user(hysteria={"auth": shared_auth}),
            "d": _user(hysteria2={"password": shared_password}, vless={"id": vless, "flow": ""}),
            "e": _user(vless={"id": _route_twin(vless), "flow": ""}),
            "alone": _user(),
            "blank": _user(hysteria=None, hysteria2={"password": ""}),
        },
    )
    before = _settings(path)

    records = []
    handler = logging.Handler(level=logging.WARNING)
    handler.emit = records.append
    migration_logger = logging.getLogger("alembic.runtime.migration")
    migration_logger.addHandler(handler)
    try:
        _run(upgrade, config, CLAIMS)
    finally:
        migration_logger.removeHandler(handler)

    after = _settings(path)
    for name in ("a", "b", "c"):
        assert after[ids[name]]["hysteria"]["auth"] != shared_auth
    assert len({after[ids[name]]["hysteria"]["auth"] for name in ("a", "b", "c")}) == 3
    for name in ("b", "d"):
        assert after[ids[name]]["hysteria2"]["password"] != shared_password
    assert after[ids["d"]]["vless"]["id"] != vless
    assert after[ids["e"]]["vless"]["id"] != _route_twin(vless)
    assert after[ids["alone"]] == before[ids["alone"]]
    assert after[ids["a"]]["vless"] == before[ids["a"]]["vless"]
    assert after[ids["d"]]["hysteria"] == before[ids["d"]]["hysteria"]

    claims = _claims(path)
    keys = [(kind, digest) for kind, digest, _, _ in claims]
    assert len(keys) == len(set(keys))
    tombstones = {(kind, digest) for kind, digest, user_id, retired_at in claims if user_id is None}
    assert (ProxySecretField.hysteria_auth.value, secret_digest(ProxySecretField.hysteria_auth, shared_auth)) in tombstones
    assert (
        ProxySecretField.hysteria2_password.value,
        secret_digest(ProxySecretField.hysteria2_password, shared_password),
    ) in tombstones
    active = {(kind, user_id): digest for kind, digest, user_id, retired_at in claims if user_id is not None}
    for user_id, settings in after.items():
        auth = (settings.get("hysteria") or {}).get("auth")
        if auth:
            assert active[(ProxySecretField.hysteria_auth.value, user_id)] == secret_digest(
                ProxySecretField.hysteria_auth, auth
            )
    assert (ProxySecretField.hysteria_auth.value, ids["blank"]) not in active
    assert (ProxySecretField.hysteria2_password.value, ids["blank"]) not in active

    report = " ".join(record.getMessage() for record in records if "proxy secret claims" in record.getMessage())
    assert "'hysteria.auth': 1" in report
    assert "'hysteria.auth': 3" in report
    assert "'vless.id': 2" in report


def test_the_claims_migration_downgrades_cleanly(tmp_path):
    path = tmp_path / "down.db"
    config = _config(path)
    _run(upgrade, config, CLAIMS)
    _run(downgrade, config, BEFORE_CLAIMS)

    connection = sqlite3.connect(path)
    try:
        tables = {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()
    assert "proxy_secret_claims" not in tables
