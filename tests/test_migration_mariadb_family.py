import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql.dml import Update

VERSIONS = Path(__file__).resolve().parents[1] / "app" / "db" / "migrations" / "versions"
MARIADB = create_async_engine("mariadb+asyncmy://user:secret@localhost/panel").dialect.name
MYSQL = create_async_engine("mysql+asyncmy://user:secret@localhost/panel").dialect.name


def _load(filename):
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, rows=(), value=None):
        self._rows = list(rows)
        self._value = value

    def __iter__(self):
        return iter(self._rows)

    def scalar(self):
        return self._value


class _Bind:
    def __init__(self, dialect_name, stored):
        self.dialect = SimpleNamespace(name=dialect_name)
        self._stored = stored
        self.statements = []
        self.written = []

    def execute(self, statement, *args, **kwargs):
        self.statements.append(" ".join(str(statement).split()))
        if isinstance(statement, Update):
            self.written.append(dict(statement.compile().params))
            return _Result()
        if str(statement).lstrip().startswith("SELECT id FROM"):
            return _Result(rows=[(1,)])
        return _Result(value=self._stored)


def test_a_mariadb_url_names_its_own_dialect():
    assert (MARIADB, MYSQL) == ("mariadb", "mysql")


@pytest.mark.parametrize("dialect_name", [MYSQL, MARIADB])
def test_the_keepalive_clamp_runs_on_the_whole_mysql_family(monkeypatch, dialect_name):
    module = _load("0374ac83604b_clamp_wireguard_keepalive.py")
    bind = _Bind(dialect_name, {"keepalive_seconds": 86400, "mtu": 1280})
    monkeypatch.setattr(module.op, "get_bind", lambda: bind, raising=False)

    module.upgrade()

    assert any("JSON_EXTRACT(wireguard_overrides, '$.keepalive_seconds')" in sql for sql in bind.statements), (
        bind.statements
    )
    assert [row["wireguard_overrides"] for row in bind.written] == [{"keepalive_seconds": 65535, "mtu": 1280}]


@pytest.mark.parametrize("dialect_name", [MYSQL, MARIADB])
def test_the_flow_cleanup_runs_on_the_whole_mysql_family(dialect_name):
    module = _load("6698b9a695e3_sanitize_user_node_inputs.py")
    bind = _Bind(dialect_name, {"vless": {"id": "u", "flow": "xtls-rprx-vision-udp"}, "trojan": {"password": "p"}})

    module._clear_unusable_flows(bind)

    assert any("JSON_EXTRACT(proxy_settings, '$.vless.flow')" in sql for sql in bind.statements), bind.statements
    assert [row["proxy_settings"] for row in bind.written] == [
        {"vless": {"id": "u", "flow": None}, "trojan": {"password": "p"}}
    ]


@pytest.mark.parametrize(
    ("filename", "stored", "call"),
    [
        ("0374ac83604b_clamp_wireguard_keepalive.py", {"keepalive_seconds": 70000}, "upgrade"),
        ("6698b9a695e3_sanitize_user_node_inputs.py", {"vless": {"flow": 5}}, "_clear_unusable_flows"),
    ],
)
def test_mariadb_issues_exactly_what_mysql_issues(monkeypatch, filename, stored, call):
    issued = {}
    for dialect_name in (MYSQL, MARIADB):
        module = _load(filename)
        bind = _Bind(dialect_name, stored)
        monkeypatch.setattr(module.op, "get_bind", lambda bind=bind: bind, raising=False)
        if call == "upgrade":
            module.upgrade()
        else:
            getattr(module, call)(bind)
        issued[dialect_name] = (bind.statements, bind.written)
    assert issued[MARIADB] == issued[MYSQL]
    assert issued[MARIADB][1]
