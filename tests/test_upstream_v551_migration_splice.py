import contextlib
import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.command import check, downgrade, upgrade
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.fork.db_rollback import current_heads, default_config, rollback

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "app" / "db" / "migrations" / "versions"
HEAD = "b708c8d4fc31"
V551_MERGE = "2e045b99a67e"
FORK_HEAD = "e79833e2a0b2"
UPSTREAM_V551_TIP = "c8d2e4f6a1b3"
UPSTREAM_TIP = "1a2b3c4d5e6f"
LOOPBACK_UPSTREAM = "a8c2d491e705"
PROD_REVISION = "d8b2c4f60a17"
JSONB_CONVERSION = "f7c63e209a37"
UPSTREAM_V551_BLOBS = {
    "b7e2c4d91f60_add_user_list_sort_index.py": "974fb3807b239f8675fb5dd28553f0fac28cb7ea",
    "86f7c2a14d3e_add_subscription_update_created_index.py": "4c1f61dc199af7c2169a1e9bfa58afc00d6c75c1",
    "d73f8a2c4e91_add_sort_order_to_nodes_and_cores.py": "c73016e1f1ad5e20cdca7ba9fc15498c334712a8",
    "a4d8c7e91b32_add_reverse_group_membership_indexes.py": "bb02a0c1eff03a614ca45e7e1157ac37f394fddf",
    "a8c2d491e705_bind_xray_client_inbounds_to_loopback.py": "3e1a14f6fc4c5213ad25c6f1744417d9d59f1491",
    "c8d2e4f6a1b3_add_masque_settings_to_proxy_settings.py": "47a904d4cd19ec6f67e9443f74a11c1c0c6eba3d",
}
UPSTREAM_V560_BLOBS = {
    "b849540d85bb_move_runtime_env_vars_to_settings.py": "33c963056749a9d84b90d1c2162abe59444b8bbb",
    "e5a7c3f9b214_add_node_outbound_usages.py": "237de1e6ca8976acd6cdcbfadcdffcb5724700ad",
    "f0420da90ff1_add_cleanup_retention_settings.py": "a94abd93da28cbe378a5f2689c0dfaeda138d11c",
    "1a2b3c4d5e6f_api_key_request_limit.py": "34247848db1b8c0d98bf4855d7501e166648a335",
}
UPSTREAM_V560_CHAIN = ["b849540d85bb", "e5a7c3f9b214", "f0420da90ff1", UPSTREAM_TIP]
SYNC_DRIVERS = {
    "sqlite": "sqlite",
    "mysql": "mysql+pymysql",
    "mariadb": "mariadb+pymysql",
    "postgresql": "postgresql+psycopg",
}
EXPOSED = '{"inbounds": [{"tag": "socks", "listen": "0.0.0.0", "port": 10808}], "outbounds": []}'
LOOPBACK = '{"inbounds": [{"tag": "socks", "listen": "127.0.0.1", "port": 10808}], "outbounds": []}'


def _sync(url: str) -> str:
    parsed = make_url(url)
    return parsed.set(drivername=SYNC_DRIVERS[parsed.get_backend_name()]).render_as_string(hide_password=False)


@contextlib.contextmanager
def _scratch_database(tmp_path, name: str):
    server = os.environ.get("SPLICE_SCRATCH_URL")
    if not server:
        yield f"sqlite+aiosqlite:///{tmp_path / f'{name}.sqlite3'}"
        return
    database = f"splice_{name}_{uuid.uuid4().hex[:8]}"
    admin = create_engine(_sync(server), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {database}"))
    try:
        yield make_url(server).set(database=database).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE {database}"))
        admin.dispose()


def _config(url: str) -> Config:
    return default_config(url)


def _quiet(action, *args, **kwargs):
    states = {
        logger: logger.disabled
        for logger in logging.root.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    }
    try:
        return action(*args, **kwargs)
    finally:
        for logger, disabled in states.items():
            logger.disabled = disabled


def _versions(url: str) -> list[str]:
    engine = create_engine(_sync(url))
    try:
        with engine.connect() as connection:
            return sorted(row[0] for row in connection.execute(text("SELECT version_num FROM alembic_version")))
    finally:
        engine.dispose()


def _schema(url: str) -> dict:
    engine = create_engine(_sync(url))
    try:
        inspector = inspect(engine)
        shape = {}
        for table in inspector.get_table_names():
            if table == "alembic_version":
                continue
            shape[table] = (
                sorted(
                    (column["name"], str(column["type"]), column["nullable"], str(column.get("default")))
                    for column in inspector.get_columns(table)
                ),
                sorted(
                    (index["name"], tuple(index["column_names"]), bool(index["unique"]))
                    for index in inspector.get_indexes(table)
                ),
                sorted(
                    (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                    for fk in inspector.get_foreign_keys(table)
                ),
                sorted((tuple(constraint["column_names"]),) for constraint in inspector.get_unique_constraints(table)),
            )
        return shape
    finally:
        engine.dispose()


def _execute(url: str, statement: str) -> None:
    engine = create_engine(_sync(url))
    try:
        with engine.begin() as connection:
            connection.execute(text(statement))
    finally:
        engine.dispose()


def _scalar_rows(url: str, statement: str) -> list:
    engine = create_engine(_sync(url))
    try:
        with engine.connect() as connection:
            return connection.execute(text(statement)).all()
    finally:
        engine.dispose()


def _settings_literal(settings: dict) -> str:
    return json.dumps(settings).replace("'", "''")


def _seed_user(url: str, username: str, settings: dict) -> None:
    _execute(
        url,
        "INSERT INTO users (username, status, used_traffic, data_limit_reset_strategy, proxy_settings, created_at) "
        f"VALUES ('{username}', 'active', 0, 'no_reset', '{_settings_literal(settings)}', '2026-08-08 00:00:00')",
    )


def _user_settings(url: str, username: str) -> dict:
    (value,) = _scalar_rows(url, f"SELECT proxy_settings FROM users WHERE username = '{username}'")[0]
    return json.loads(value) if isinstance(value, str) else value


def _seed_template(url: str, name: str, content: str, *, system: bool) -> None:
    flag = "true" if make_url(url).get_backend_name() == "postgresql" else "1"
    system_flag = flag if system else ("false" if flag == "true" else "0")
    _execute(
        url,
        "INSERT INTO client_templates (name, template_type, content, is_default, is_system) "
        f"VALUES ('{name}', 'xray_subscription', '{content}', {system_flag}, {system_flag})",
    )


def _templates(url: str) -> dict[str, str]:
    return dict(
        _scalar_rows(url, "SELECT name, content FROM client_templates WHERE template_type = 'xray_subscription'")
    )


def _git_blob(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def test_the_graph_has_one_head_that_merges_the_fork_head_with_the_upstream_v560_tip():
    script = ScriptDirectory.from_config(default_config())

    assert script.get_heads() == [HEAD]
    assert script.get_revision(HEAD).down_revision == (V551_MERGE, UPSTREAM_TIP)
    assert script.get_revision(V551_MERGE).down_revision == (FORK_HEAD, UPSTREAM_V551_TIP)
    assert script.get_revision("b7e2c4d91f60").down_revision == "b4c7e8f1a2d3"
    parents = [UPSTREAM_V551_TIP, *UPSTREAM_V560_CHAIN[:-1]]
    for revision, parent in zip(UPSTREAM_V560_CHAIN, parents, strict=True):
        assert script.get_revision(revision).down_revision == parent, revision


def test_the_six_upstream_v551_revisions_are_byte_identical():
    for name, blob in UPSTREAM_V551_BLOBS.items():
        assert _git_blob(VERSIONS / name) == blob, name


def test_the_v551_merge_revision_keeps_its_released_parents_and_content():
    assert _git_blob(VERSIONS / "2e045b99a67e_merge_upstream_v5_5_1_migrations_with_fork_head.py") == (
        "8afb48c62b0f7e5337cc0421d392287cea02ed4b"
    )


@pytest.mark.parametrize(("merge", "parent"), [(HEAD, V551_MERGE), (V551_MERGE, FORK_HEAD)])
def test_a_plain_downgrade_of_either_merge_revision_is_refused(tmp_path, merge, parent):
    with _scratch_database(tmp_path, "refuse") as url:
        config = _config(url)
        _quiet(upgrade, config, "head")
        if merge == V551_MERGE:
            _quiet(rollback, config, V551_MERGE, echo=lambda _line: None)
            assert _versions(url) == [V551_MERGE]
        before = _schema(url)

        with pytest.raises(RuntimeError, match="python -m app.fork.db_rollback"):
            _quiet(downgrade, config, parent)

        assert _versions(url) == [merge]
        assert _schema(url) == before


@pytest.mark.parametrize("stamp", [UPSTREAM_TIP, V551_MERGE], ids=["official-v560", "fork-v551-merge"])
def test_a_database_stamped_at_an_earlier_tip_gets_every_missing_revision(tmp_path, stamp):
    script = ScriptDirectory.from_config(default_config())
    with _scratch_database(tmp_path, "stamped") as url, _scratch_database(tmp_path, "fresh") as fresh:
        _quiet(upgrade, _config(url), stamp)
        _quiet(upgrade, _config(fresh), "head")
        applied = {rev.revision for rev in script.iterate_revisions(stamp, "base")}
        missing = {rev.revision for rev in script.iterate_revisions(HEAD, "base")} - applied

        _quiet(upgrade, _config(url), "head")

        assert missing
        if stamp == UPSTREAM_TIP:
            assert {FORK_HEAD, V551_MERGE, HEAD} <= missing
        else:
            assert set(UPSTREAM_V560_CHAIN) | {HEAD} == missing
        assert _versions(url) == [HEAD]
        assert _schema(url) == _schema(fresh)


def _filler(column) -> object:
    enums = getattr(column["type"], "enums", None)
    if enums:
        return enums[0]
    kind = str(column["type"]).upper()
    if "INT" in kind:
        return 1
    if "BOOL" in kind:
        return 0
    if any(word in kind for word in ("FLOAT", "REAL", "NUMERIC", "DECIMAL", "DOUBLE")):
        return 1.0
    if "DATE" in kind or "TIME" in kind:
        return "2026-08-08 00:00:00"
    if "JSON" in kind:
        return "{}"
    return "fixture"


def _insert(url: str, table: str, values: dict) -> dict:
    engine = create_engine(_sync(url))
    try:
        row = dict(values)
        for column in inspect(engine).get_columns(table):
            if column["name"] in row or column["nullable"] or column.get("default") is not None:
                continue
            if column.get("autoincrement") is True or column["name"] == "id":
                continue
            row[column["name"]] = _filler(column)
        names = ", ".join(row)
        marks = ", ".join(f":{name}" for name in row)
        with engine.begin() as connection:
            connection.execute(text(f"INSERT INTO {table} ({names}) VALUES ({marks})"), row)
        return row
    finally:
        engine.dispose()


def _kept(stored, value) -> bool:
    if isinstance(stored, datetime):
        if stored.tzinfo is not None:
            stored = stored.astimezone(UTC).replace(tzinfo=None)
        return stored == datetime.fromisoformat(str(value))
    return str(stored) == str(value)


FIXTURE = {
    "admins": {"id": 7, "username": "fixture_admin", "hashed_password": "fixture_hash"},
    "users": {"id": 21, "username": "fixture_user", "proxy_settings": '{"vless": {"id": "fixture-vless"}}'},
    "nodes": {"id": 11, "name": "fixture_node", "address": "192.0.2.1", "port": 62050},
    "node_stats": {"id": 3, "node_id": 11, "mem_used": 50},
    "api_keys": {"id": 5, "admin_id": 7, "name": "fixture_key", "key_hash": "fixture_key_hash"},
}


def test_the_production_revision_upgrade_keeps_rows_and_sets_upstream_defaults(tmp_path):
    with _scratch_database(tmp_path, "fixture") as url:
        config = _config(url)
        _quiet(upgrade, config, PROD_REVISION)
        written = {table: _insert(url, table, values) for table, values in FIXTURE.items()}
        before = {table: _scalar_rows(url, f"SELECT * FROM {table} ORDER BY id") for table in FIXTURE}
        had_settings = bool(_scalar_rows(url, "SELECT id FROM settings"))

        _quiet(upgrade, config, "head")

        assert _versions(url) == [HEAD]
        for table, values in written.items():
            rows = _scalar_rows(url, f"SELECT {', '.join(values)} FROM {table} ORDER BY id")
            assert len(rows) == len(before[table]) == 1, table
            stored = dict(zip(values, rows[0], strict=True))
            for name, value in values.items():
                if name == "proxy_settings":
                    kept = json.loads(stored[name]) if isinstance(stored[name], str) else stored[name]
                    assert kept["vless"] == json.loads(value)["vless"], (table, name)
                else:
                    assert _kept(stored[name], value), (table, name, stored[name], value)
        assert _scalar_rows(url, "SELECT max_requests, request_count, delete_on_limit FROM api_keys WHERE id = 5") == [
            (None, 0, 0)
        ]
        if had_settings:
            for (cleanup,) in _scalar_rows(url, "SELECT cleanup FROM settings"):
                assert isinstance(json.loads(cleanup) if isinstance(cleanup, str) else cleanup, dict)
        assert _scalar_rows(url, "SELECT COUNT(*) FROM node_outbound_usages") == [(0,)]
        _quiet(check, config)


def test_the_four_upstream_v560_revisions_are_byte_identical():
    for name, blob in UPSTREAM_V560_BLOBS.items():
        assert (VERSIONS / name).is_file(), name
        assert _git_blob(VERSIONS / name) == blob, name


def test_a_fresh_install_and_the_production_upgrade_path_reach_the_same_schema(tmp_path):
    with _scratch_database(tmp_path, "fresh") as fresh, _scratch_database(tmp_path, "path") as path:
        _quiet(upgrade, _config(fresh), "head")
        for revision in (PROD_REVISION, FORK_HEAD, "head"):
            _quiet(upgrade, _config(path), revision)

        assert _versions(fresh) == _versions(path) == [HEAD]
        fresh_schema = _schema(fresh)
        assert fresh_schema == _schema(path)
        _quiet(check, _config(fresh))
        _quiet(check, _config(path))

    outbound_columns, outbound_indexes, outbound_keys, outbound_unique = fresh_schema["node_outbound_usages"]
    assert {column[0] for column in outbound_columns} == {
        "id",
        "created_at",
        "node_id",
        "outbound_tag",
        "uplink",
        "downlink",
    }
    assert ("ix_node_outbound_usages_node_id_created_at", ("node_id", "created_at"), False) in outbound_indexes
    assert outbound_keys == [(("node_id",), "nodes", ("id",))]
    assert (("created_at", "node_id", "outbound_tag"),) in outbound_unique
    node_stats_indexes = fresh_schema["node_stats"][1]
    assert ("ix_node_stats_created_at", ("created_at",), False) in node_stats_indexes
    assert ("ix_node_stats_node_id_created_at", ("node_id", "created_at"), False) in node_stats_indexes
    settings_columns = {column[0]: column for column in fresh_schema["settings"][0]}
    assert settings_columns["cleanup"][2] is False
    api_key_columns = {column[0]: column for column in fresh_schema["api_keys"][0]}
    assert api_key_columns["max_requests"][2] is True
    assert api_key_columns["request_count"][2] is False
    assert api_key_columns["delete_on_limit"][2] is False


@pytest.mark.parametrize("masque_first", [True, False], ids=["masque-before-jsonb", "masque-after-jsonb"])
def test_masque_passwords_survive_either_order_of_the_two_branches(tmp_path, masque_first):
    script = ScriptDirectory.from_config(default_config())
    assert JSONB_CONVERSION in {rev.revision for rev in script.iterate_revisions(FORK_HEAD, "base")}
    assert JSONB_CONVERSION not in {rev.revision for rev in script.iterate_revisions(LOOPBACK_UPSTREAM, "base")}
    settings = {"vless": {"id": str(uuid.uuid4()), "flow": None}, "trojan": {"password": "kept-trojan-secret"}}
    with _scratch_database(tmp_path, "order") as url:
        config = _config(url)
        first = LOOPBACK_UPSTREAM if masque_first else FORK_HEAD
        _quiet(upgrade, config, first)
        _seed_user(url, "masque_order", settings)
        if masque_first:
            _quiet(upgrade, config, UPSTREAM_V551_TIP)
            generated = _user_settings(url, "masque_order")["masque"]["password"]
        _quiet(upgrade, config, "head")

        stored = _user_settings(url, "masque_order")
        assert _versions(url) == [HEAD]
        assert isinstance(stored["masque"]["password"], str)
        assert stored["masque"]["password"]
        if masque_first:
            assert stored["masque"]["password"] == generated
        assert stored["vless"]["id"] == settings["vless"]["id"]
        assert stored["trojan"]["password"] == "kept-trojan-secret"


def test_the_upstream_loopback_rewrite_after_the_fork_one_leaves_templates_byte_equal(tmp_path):
    with _scratch_database(tmp_path, "loopback") as url:
        config = _config(url)
        _quiet(upgrade, config, "fd33c6013baa")
        _seed_template(url, "exposed_system", EXPOSED, system=True)
        _seed_template(url, "exposed_custom", EXPOSED, system=False)
        _quiet(upgrade, config, FORK_HEAD)
        after_fork_rewrite = _templates(url)
        assert after_fork_rewrite["exposed_system"] == LOOPBACK
        assert after_fork_rewrite["exposed_custom"] == EXPOSED

        _quiet(upgrade, config, "head")
        assert _templates(url) == after_fork_rewrite

        _quiet(rollback, config, FORK_HEAD, echo=lambda _line: None)
        assert _versions(url) == [FORK_HEAD]
        assert _templates(url) == after_fork_rewrite


def test_a_system_template_reopened_after_the_fork_rewrite_is_closed_again_by_the_upstream_one(tmp_path):
    with _scratch_database(tmp_path, "reopened") as url:
        config = _config(url)
        _quiet(upgrade, config, FORK_HEAD)
        _seed_template(url, "reopened_system", EXPOSED, system=True)

        _quiet(upgrade, config, "head")

        assert _templates(url)["reopened_system"] == LOOPBACK


def test_downgrade_to_the_fork_head_and_to_production_then_upgrade_again(tmp_path):
    with (
        _scratch_database(tmp_path, "down") as url,
        _scratch_database(tmp_path, "forkhead") as fork_head,
        _scratch_database(tmp_path, "prod") as prod,
        _scratch_database(tmp_path, "fresh") as fresh,
    ):
        config = _config(url)
        _quiet(upgrade, _config(fork_head), FORK_HEAD)
        _quiet(upgrade, _config(prod), PROD_REVISION)
        _quiet(upgrade, _config(fresh), "head")
        _quiet(upgrade, config, "head")
        _seed_user(url, "masque_down", {"masque": {"password": "set-before-the-downgrade"}})

        with pytest.raises(RuntimeError, match="python -m app.fork.db_rollback"):
            _quiet(downgrade, config, FORK_HEAD)
        assert _versions(url) == [HEAD]

        _quiet(rollback, config, FORK_HEAD, echo=lambda _line: None)
        assert current_heads(config) == (FORK_HEAD,)
        assert _schema(url) == _schema(fork_head)
        assert "password" not in _user_settings(url, "masque_down")["masque"]

        _quiet(upgrade, config, "head")
        assert _schema(url) == _schema(fresh)
        assert _user_settings(url, "masque_down")["masque"]["password"]

        _quiet(rollback, config, PROD_REVISION, echo=lambda _line: None)
        assert _versions(url) == [PROD_REVISION]
        assert _schema(url) == _schema(prod)

        _quiet(upgrade, config, "head")
        assert _versions(url) == [HEAD]
        assert _schema(url) == _schema(fresh)
