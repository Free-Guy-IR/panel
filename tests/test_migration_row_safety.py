import json
from argparse import Namespace

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, text


def _run(url, action, revision, *x_arguments):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    if x_arguments:
        config.cmd_opts = Namespace(x=list(x_arguments))
    with create_engine(url).begin() as connection:
        config.attributes["connection"] = connection
        action(config, revision)


def _revision(url):
    with create_engine(url).connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _seed_user(connection, user_id=1, proxy_settings="{}", **columns):
    names = ["id", "username", "status", "used_traffic", "data_limit_reset_strategy", "proxy_settings", "created_at"]
    values = [user_id, f"u{user_id}", "active", 0, "no_reset", proxy_settings, "2026-08-08 00:00:00"]
    for name, value in columns.items():
        names.append(name)
        values.append(value)
    placeholders = ", ".join(f":p{index}" for index in range(len(values)))
    connection.execute(
        text(f"INSERT INTO users ({', '.join(names)}) VALUES ({placeholders})"),
        {f"p{index}": value for index, value in enumerate(values)},
    )


def test_limit_applied_upgrade_survives_an_existing_state_row(tmp_path):
    url = f"sqlite:///{tmp_path / 'limiter.sqlite3'}"
    _run(url, upgrade, "f174c3b13642")
    with create_engine(url).begin() as connection:
        _seed_user(connection)
        connection.execute(
            text(
                "INSERT INTO user_connection_states "
                "(user_id, checked_at, devices, address_sources, hwid_count, node_count, app_count, verdict, streak) "
                "VALUES (1, '2026-08-08 20:00:00', 1, 1, 0, 1, 1, 'within_limit', 0)"
            )
        )

    _run(url, upgrade, "9338a6417365")

    with create_engine(url).connect() as connection:
        assert connection.execute(text("SELECT limit_applied FROM user_connection_states")).scalar() == 0


def _usage_counts(url):
    with create_engine(url).connect() as connection:
        return (
            connection.execute(text("SELECT COUNT(*) FROM node_user_usages")).scalar(),
            connection.execute(text("SELECT COUNT(*) FROM node_usages")).scalar(),
        )


def test_node_delete_rollback_keeps_the_usage_history(tmp_path):
    url = f"sqlite:///{tmp_path / 'history.sqlite3'}"
    _run(url, upgrade, "3f7a9c2e1b45")
    with create_engine(url).begin() as connection:
        _seed_user(connection)
        connection.execute(
            text(
                "INSERT INTO node_user_usages (created_at, user_id, node_id, used_traffic) "
                "VALUES ('2026-09-11 10:00:00', 1, NULL, 123456789)"
            )
        )
        connection.execute(
            text("INSERT INTO node_usages (created_at, node_id, uplink, downlink) VALUES ('2026-09-11 10:00:00', NULL, 5, 7)")
        )

    with pytest.raises(RuntimeError, match="allow_data_loss=true"):
        _run(url, downgrade, "e8c1f04b72a9")

    kept = _usage_counts(url)
    assert kept == (1, 1)
    assert _revision(url) == "3f7a9c2e1b45"


def test_node_delete_rollback_deletes_the_history_only_with_explicit_consent(tmp_path):
    url = f"sqlite:///{tmp_path / 'consent.sqlite3'}"
    _run(url, upgrade, "3f7a9c2e1b45")
    with create_engine(url).begin() as connection:
        _seed_user(connection)
        connection.execute(
            text("INSERT INTO node_usages (created_at, node_id, uplink, downlink) VALUES ('2026-09-11 10:00:00', NULL, 5, 7)")
        )

    _run(url, downgrade, "e8c1f04b72a9", "allow_data_loss=true")

    assert _usage_counts(url) == (0, 0)
    assert _revision(url) == "e8c1f04b72a9"


def test_node_delete_rollback_without_orphaned_history_needs_no_consent(tmp_path):
    url = f"sqlite:///{tmp_path / 'clean.sqlite3'}"
    _run(url, upgrade, "3f7a9c2e1b45")

    _run(url, downgrade, "e8c1f04b72a9")

    assert _revision(url) == "e8c1f04b72a9"


def test_fleet_wide_content_filter_rollback_is_refused_without_consent(tmp_path):
    url = f"sqlite:///{tmp_path / 'filters.sqlite3'}"
    _run(url, upgrade, "d4a92bc8e531")
    with create_engine(url).begin() as connection:
        connection.execute(
            text(
                "INSERT INTO content_filter_profiles (id, name, created_at, updated_at, strict_mode) "
                "VALUES (1, 'p', '2026-09-15 00:00:00', '2026-09-15 00:00:00', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO content_filter_assignments (profile_id, node_id, inbound_tag) "
                "VALUES (1, NULL, '')"
            )
        )

    with pytest.raises(RuntimeError, match="allow_data_loss=true"):
        _run(url, downgrade, "c3f81ab7d420")
    with create_engine(url).connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM content_filter_assignments")).scalar() == 1
    assert _revision(url) == "d4a92bc8e531"

    _run(url, downgrade, "c3f81ab7d420", "allow_data_loss=true")
    with create_engine(url).connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM content_filter_assignments")).scalar() == 0
    assert _revision(url) == "c3f81ab7d420"


def test_openvpn_core_type_rollback_is_refused_while_openvpn_cores_exist(tmp_path):
    url = f"sqlite:///{tmp_path / 'openvpn.sqlite3'}"
    _run(url, upgrade, "8a1f3c6e9d02")
    with create_engine(url).begin() as connection:
        connection.execute(
            text(
                "INSERT INTO core_configs (id, created_at, name, config, type) "
                "VALUES (900, '2026-07-30 00:00:00', 'ovpn', '{}', 'openvpn')"
            )
        )

    with pytest.raises(RuntimeError, match="OpenVPN core"):
        _run(url, downgrade, "f976bfcf4738")
    assert _revision(url) == "8a1f3c6e9d02"


SANITIZE_REVISION = "6698b9a695e3"
SANITIZE_PARENT = "b176d23e434c"


def _proxy_settings(flow=..., **extra):
    settings = {"vmess": {"id": "b3b08bab-ceea-43b9-8e31-92058be69917"}, "vless": {"id": "80c60e6c-a23e-4fd3-a4d4-b99ec6866c4a"}}
    if flow is not ...:
        settings["vless"]["flow"] = flow
    settings.update(extra)
    return json.dumps(settings)


def test_sanitize_migration_clears_only_unusable_values(tmp_path):
    url = f"sqlite:///{tmp_path / 'sanitize.sqlite3'}"
    _run(url, upgrade, SANITIZE_PARENT)
    seeded = {
        1: _proxy_settings("xtls-rprx-vision"),
        2: _proxy_settings(""),
        3: _proxy_settings(None),
        4: _proxy_settings(),
        5: _proxy_settings("xtls-rprx-vision-udp", trojan={"password": "keep"}),
        6: _proxy_settings(5),
        7: "not json",
        8: json.dumps("double encoded"),
        9: _proxy_settings("xtls-rprx-vision-udp443"),
    }
    with create_engine(url).begin() as connection:
        _seed_user(connection, 1, seeded[1], hwid_limit=-1, auto_delete_in_days=99_999_999)
        _seed_user(connection, 2, seeded[2], hwid_limit=5, auto_delete_in_days=30)
        _seed_user(connection, 3, seeded[3], hwid_limit=2**31 + 5, auto_delete_in_days=-1)
        _seed_user(connection, 4, seeded[4], hwid_limit=None, auto_delete_in_days=36500)
        for user_id in (5, 6, 7, 8, 9):
            _seed_user(connection, user_id, seeded[user_id])
        connection.execute(
            text(
                "INSERT INTO user_templates (id, name, data_limit, expire_duration, username_prefix, username_suffix, "
                "hwid_limit, data_limit_reset_strategy) VALUES "
                "(1, 't1', 0, 0, '', '', -3, 'no_reset'), (2, 't2', 0, 0, '', '', 4, 'no_reset')"
            )
        )

    _run(url, upgrade, SANITIZE_REVISION)

    with create_engine(url).connect() as connection:
        rows = {
            row.id: row
            for row in connection.execute(text("SELECT id, proxy_settings, hwid_limit, auto_delete_in_days FROM users"))
        }
        templates = dict(connection.execute(text("SELECT id, hwid_limit FROM user_templates")).all())

    for untouched in (1, 2, 3, 4, 7, 8, 9):
        assert rows[untouched].proxy_settings == seeded[untouched], untouched
    cleared = json.loads(rows[5].proxy_settings)
    assert cleared["vless"] == {"id": "80c60e6c-a23e-4fd3-a4d4-b99ec6866c4a", "flow": None}
    assert cleared["trojan"] == {"password": "keep"}
    assert json.loads(rows[6].proxy_settings)["vless"]["flow"] is None
    assert [rows[user_id].hwid_limit for user_id in (1, 2, 3, 4)] == [2**31 - 1, 5, 2**31 - 1, None]
    assert [rows[user_id].auto_delete_in_days for user_id in (1, 2, 3, 4)] == [36500, 30, -1, 36500]
    assert templates == {1: 2**31 - 1, 2: 4}


def test_sanitize_migration_changes_nothing_on_clean_data(tmp_path):
    url = f"sqlite:///{tmp_path / 'clean_sanitize.sqlite3'}"
    _run(url, upgrade, SANITIZE_PARENT)
    with create_engine(url).begin() as connection:
        _seed_user(connection, 1, _proxy_settings(None), hwid_limit=3, auto_delete_in_days=7)
        before = connection.execute(text("SELECT * FROM users")).all()

    _run(url, upgrade, SANITIZE_REVISION)
    _run(url, downgrade, SANITIZE_PARENT)
    _run(url, upgrade, SANITIZE_REVISION)

    with create_engine(url).connect() as connection:
        assert connection.execute(text("SELECT * FROM users")).all() == before


KEEPALIVE_REVISION = "0374ac83604b"
KEEPALIVE_PARENT = "6d8eb8fba21b"


def test_keepalive_migration_clamps_only_values_wireguard_refuses(tmp_path):
    url = f"sqlite:///{tmp_path / 'keepalive.sqlite3'}"
    _run(url, upgrade, KEEPALIVE_PARENT)
    seeded = {
        1: json.dumps({"keepalive_seconds": 86400, "mtu": 1280}),
        2: json.dumps({"keepalive_seconds": 65536}),
        3: json.dumps({"keepalive_seconds": 65535}),
        4: json.dumps({"keepalive_seconds": 25, "dns": ["1.1.1.1"]}),
        5: json.dumps({"mtu": 1420}),
        6: None,
        7: "not json",
    }
    with create_engine(url).begin() as connection:
        for host_id, overrides in seeded.items():
            connection.execute(
                text(
                    "INSERT INTO hosts (id, remark, address, priority, wireguard_overrides) "
                    "VALUES (:id, :remark, 'wg.example.com', 1, :overrides)"
                ),
                {"id": host_id, "remark": f"h{host_id}", "overrides": overrides},
            )

    _run(url, upgrade, KEEPALIVE_REVISION)

    with create_engine(url).connect() as connection:
        stored = dict(connection.execute(text("SELECT id, wireguard_overrides FROM hosts")).all())
    assert json.loads(stored[1]) == {"keepalive_seconds": 65535, "mtu": 1280}
    assert json.loads(stored[2]) == {"keepalive_seconds": 65535}
    for untouched in (3, 4, 5, 6, 7):
        assert stored[untouched] == seeded[untouched], untouched

    _run(url, downgrade, KEEPALIVE_PARENT)
    assert _revision(url) == KEEPALIVE_PARENT
    with create_engine(url).connect() as connection:
        assert json.loads(connection.execute(text("SELECT wireguard_overrides FROM hosts WHERE id = 1")).scalar()) == {
            "keepalive_seconds": 65535,
            "mtu": 1280,
        }
