import contextlib
import logging
import os
import uuid

from alembic.command import downgrade, upgrade
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.models import Admin, AdminRole, APIKey
from app.fork.db_rollback import default_config

BEFORE = "b708c8d4fc31"
HOSTS_DELETE = "2d198a5a8068"
SYNC_DRIVERS = {
    "sqlite": "sqlite",
    "mysql": "mysql+pymysql",
    "mariadb": "mariadb+pymysql",
    "postgresql": "postgresql+psycopg",
}
PRODUCTION_ROLES = {
    "owner": {"hosts": {"read": True, "create": True, "update": True}, "users": {"read": {"scope": 2}}},
    "administrator": {"hosts": {"read": True, "create": True, "update": True}},
    "operator": {"users": {"read": {"scope": 1}}, "hwids": {"read": True, "delete": True}},
}
EXTRA_ROLES = {
    "editor_off": {"hosts": {"read": True, "update": False}},
    "editor_scoped": {"hosts": {"read": True, "update": {"scope": 2}}},
    "editor_denied": {"hosts": {"read": True, "update": {"scope": 0}}},
    "already_split": {"hosts": {"read": True, "update": True, "delete": False}},
}
KEYS = {
    "inheriting": (True, {}),
    "custom_editor": (False, {"hosts": {"read": True, "update": True}}),
    "custom_reader": (False, {"hosts": {"read": True}}),
}


def _sync(url: str) -> str:
    parsed = make_url(url)
    return parsed.set(drivername=SYNC_DRIVERS[parsed.get_backend_name()]).render_as_string(hide_password=False)


@contextlib.contextmanager
def _scratch_database(tmp_path, name: str):
    server = os.environ.get("SPLICE_SCRATCH_URL")
    if not server:
        yield f"sqlite+aiosqlite:///{tmp_path / f'{name}.sqlite3'}"
        return
    database = f"hostsdel_{name}_{uuid.uuid4().hex[:8]}"
    admin = create_engine(_sync(server), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {database}"))
    try:
        yield make_url(server).set(database=database).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE {database}"))
        admin.dispose()


def _quiet(action, *args):
    states = {
        logger: logger.disabled
        for logger in logging.root.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    }
    try:
        return action(*args)
    finally:
        for logger, disabled in states.items():
            logger.disabled = disabled


def _seed(url: str) -> None:
    engine = create_engine(_sync(url))
    try:
        with Session(engine) as session:
            roles = {role.name: role for role in session.scalars(select(AdminRole))}
            for name, permissions in PRODUCTION_ROLES.items():
                roles[name].permissions = permissions
            for name, permissions in EXTRA_ROLES.items():
                session.add(AdminRole(name=name, permissions=permissions))
            holder = Admin(username="key-holder", hashed_password="x", role_id=roles["administrator"].id)
            session.add(holder)
            session.flush()
            for name, (inherit, permissions) in KEYS.items():
                session.add(
                    APIKey(
                        admin_id=holder.id,
                        name=name,
                        key_hash=f"v1${name}$hash",
                        api_key_trimmed="pg_key_x***y",
                        permissions=permissions,
                        inherit_permissions=inherit,
                    )
                )
            session.commit()
    finally:
        engine.dispose()


def _permissions(url: str, model) -> dict[str, dict]:
    engine = create_engine(_sync(url))
    try:
        with Session(engine) as session:
            return {row.name: row.permissions for row in session.scalars(select(model))}
    finally:
        engine.dispose()


def _versions(url: str) -> list[str]:
    engine = create_engine(_sync(url))
    try:
        with engine.connect() as connection:
            return sorted(row[0] for row in connection.execute(text("SELECT version_num FROM alembic_version")))
    finally:
        engine.dispose()


def test_the_revision_chains_onto_one_line_of_history():
    script = ScriptDirectory.from_config(default_config())
    assert len(script.get_heads()) == 1
    assert script.get_revision(HOSTS_DELETE).down_revision == BEFORE
    assert HOSTS_DELETE in {revision.revision for revision in script.iterate_revisions("heads", BEFORE)}


def test_every_role_and_key_that_could_delete_hosts_keeps_that_ability_and_nothing_else_changes(tmp_path):
    with _scratch_database(tmp_path, "upgrade") as url:
        config = default_config(url)
        _quiet(upgrade, config, BEFORE)
        _seed(url)
        roles_before = _permissions(url, AdminRole)
        keys_before = _permissions(url, APIKey)

        _quiet(upgrade, config, HOSTS_DELETE)

        assert _versions(url) == [HOSTS_DELETE]
        roles = _permissions(url, AdminRole)
        keys = _permissions(url, APIKey)
        assert roles["owner"]["hosts"] == {"read": True, "create": True, "update": True, "delete": True}
        assert roles["administrator"]["hosts"] == {"read": True, "create": True, "update": True, "delete": True}
        assert roles["editor_scoped"]["hosts"]["delete"] == {"scope": 2}
        assert roles["editor_denied"]["hosts"]["delete"] == {"scope": 0}
        for unchanged in ("operator", "editor_off", "already_split"):
            assert roles[unchanged] == roles_before[unchanged]
        assert roles["owner"]["users"] == roles_before["owner"]["users"]
        assert keys["custom_editor"]["hosts"] == {"read": True, "update": True, "delete": True}
        assert keys["inheriting"] == keys_before["inheriting"]
        assert keys["custom_reader"] == keys_before["custom_reader"]


def test_the_downgrade_removes_the_delete_action_the_older_code_cannot_load(tmp_path):
    with _scratch_database(tmp_path, "downgrade") as url:
        config = default_config(url)
        _quiet(upgrade, config, BEFORE)
        _seed(url)
        roles_before = _permissions(url, AdminRole)
        keys_before = _permissions(url, APIKey)
        _quiet(upgrade, config, HOSTS_DELETE)

        _quiet(downgrade, config, BEFORE)

        assert _versions(url) == [BEFORE]
        roles = _permissions(url, AdminRole)
        for name in ("owner", "administrator", "operator", "editor_off", "editor_scoped", "editor_denied"):
            assert roles[name] == roles_before[name], name
        assert "delete" not in roles["already_split"]["hosts"]
        assert all("delete" not in (permissions.get("hosts") or {}) for permissions in roles.values())
        assert _permissions(url, APIKey) == keys_before
