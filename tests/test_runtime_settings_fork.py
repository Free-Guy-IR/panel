import importlib.util
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select

import config
from app.db.models import User, UserSubscriptionUpdate
from app.jobs import cleanup_subscription_updates, record_usages
from app.models.proxy import ProxyTable
from app.models.settings import General, Subscription
from tests.api.helpers import unique_name
from tests.test_record_usages import session_factory  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "app/db/migrations/versions/b849540d85bb_move_runtime_env_vars_to_settings.py"
MOVED_ENV_VARS = (
    "DISABLE_RECORDING_NODE_USAGE",
    "ENABLE_RECORDING_NODES_STATS",
    "RESET_USER_USAGE_CLEAN_CHART_DATA",
    "NODE_HEALTH_FAIL_THRESHOLD",
    "NODE_HEALTH_RECOVER_THRESHOLD",
    "NODE_HEALTH_PROBE_TIMEOUT",
    "NODE_HEALTH_MAX_BACKOFF",
    "STOP_NODES_ON_SHUTDOWN",
    "USER_SUBSCRIPTION_CLIENTS_LIMIT",
)


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location("move_runtime_env_vars_to_settings_fork", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _settings_db(path: Path, general: dict, subscription: dict) -> sa.Engine:
    engine = sa.create_engine(f"sqlite:///{path}")
    metadata = sa.MetaData()
    table = sa.Table(
        "settings",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("general", sa.JSON),
        sa.Column("subscription", sa.JSON),
        sa.Column("telegram", sa.JSON),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            table.insert().values(id=1, general=general, subscription=subscription, telegram={"enable": False})
        )
    return engine


def _run_upgrade(engine: sa.Engine, migration) -> dict:
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration.upgrade()
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT general, subscription, telegram FROM settings WHERE id = 1")).one()
    return {key: json.loads(value) if isinstance(value, str) else value for key, value in row._mapping.items()}


def test_upgrade_copies_every_moved_env_value_into_the_settings_row(migration, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in MOVED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISABLE_RECORDING_NODE_USAGE", "true")
    monkeypatch.setenv("ENABLE_RECORDING_NODES_STATS", "1")
    monkeypatch.setenv("RESET_USER_USAGE_CLEAN_CHART_DATA", "yes")
    monkeypatch.setenv("NODE_HEALTH_FAIL_THRESHOLD", "7")
    monkeypatch.setenv("NODE_HEALTH_RECOVER_THRESHOLD", "4")
    monkeypatch.setenv("NODE_HEALTH_PROBE_TIMEOUT", "9")
    monkeypatch.setenv("NODE_HEALTH_MAX_BACKOFF", "0")
    monkeypatch.setenv("STOP_NODES_ON_SHUTDOWN", "false")
    monkeypatch.setenv("USER_SUBSCRIPTION_CLIENTS_LIMIT", "25")
    monkeypatch.setenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "5")
    monkeypatch.setenv("DO_NOT_LOG_TELEGRAM_BOT", "false")
    engine = _settings_db(
        tmp_path / "settings.db",
        {"default_method": "aes-128-gcm", "custom_variables": None},
        {"url_prefix": "", "rules": []},
    )

    row = _run_upgrade(engine, migration)

    assert row["general"] == {
        "default_method": "aes-128-gcm",
        "custom_variables": None,
        "usage": {"record_node_usage": False, "record_node_stats": True, "reset_usage_clean_chart_data": True},
        "node_health": {"fail_threshold": 7, "recover_threshold": 4, "probe_timeout": 9, "max_backoff": 0},
        "stop_nodes_on_shutdown": False,
    }
    assert row["subscription"] == {"url_prefix": "", "rules": [], "clients_limit": 25}
    assert row["telegram"] == {"enable": False}
    general = General.model_validate(row["general"])
    assert general.usage.record_node_usage is False
    assert general.usage.record_node_stats is True
    assert general.stop_nodes_on_shutdown is False
    assert Subscription.model_validate({**row["subscription"], "rules": []}).clients_limit == 25
    assert "admin_token_expire_minutes" not in row["general"]
    assert "do_not_log_bot" not in row["telegram"]


def test_upgrade_reads_dotenv_when_the_environment_lacks_the_var(migration, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in MOVED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text('STOP_NODES_ON_SHUTDOWN = "False"\nuser_subscription_clients_limit=0\n')
    monkeypatch.setenv("USER_SUBSCRIPTION_CLIENTS_LIMIT", "3")
    engine = _settings_db(tmp_path / "settings.db", {"default_method": "aes-128-gcm"}, {"rules": []})

    row = _run_upgrade(engine, migration)

    assert row["general"] == {"default_method": "aes-128-gcm", "stop_nodes_on_shutdown": False}
    assert row["subscription"] == {"rules": [], "clients_limit": 3}


def test_upgrade_leaves_the_row_alone_when_no_moved_var_is_set(migration, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in MOVED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    engine = _settings_db(tmp_path / "settings.db", {"default_method": "aes-128-gcm"}, {"rules": []})

    row = _run_upgrade(engine, migration)

    assert row["general"] == {"default_method": "aes-128-gcm"}
    assert row["subscription"] == {"rules": []}
    general = General.model_validate(row["general"])
    assert general.usage.record_node_usage is True
    assert general.stop_nodes_on_shutdown is True


def test_config_no_longer_exposes_the_moved_env_vars():
    for name in ("feature_settings", "FeatureSettings"):
        assert not hasattr(config, name), name
    assert set(config.UsageSettings.model_fields) == {"user_usage_history_flush_interval"}
    assert isinstance(config.usage_settings, config.UsageSettings)
    assert not hasattr(config.subscription_env_settings, "clients_limit")
    assert not hasattr(config.job_settings, "node_health_fail_threshold")
    assert config.jwt_settings is not None


def test_no_app_code_reads_a_moved_var_from_the_environment():
    pattern = re.compile(
        r"\busage_settings\.(?!user_usage_history_flush_interval\b)|\bfeature_settings\b|"
        r"subscription_env_settings\.clients_limit|"
        r"disable_recording_node_usage|enable_recording_nodes_stats|reset_user_usage_clean_chart_data|"
        r"node_health_(fail|recover)_threshold|node_health_probe_timeout|node_health_max_backoff"
    )
    offenders = []
    for path in [ROOT / "config.py", ROOT / "main.py", *(ROOT / "app").rglob("*.py")]:
        if "migrations" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert offenders == []


def _serve_subscription(monkeypatch, clients_limit: int) -> None:
    async def get_subscription_settings():
        return Subscription.model_construct(clients_limit=clients_limit)

    monkeypatch.setattr(cleanup_subscription_updates, "subscription_settings", get_subscription_settings)


async def _user_with_updates(session_factory, count: int) -> int:  # noqa: F811
    now = datetime.now(UTC)
    async with session_factory() as session:
        user = User(username=unique_name("sub_updates"), proxy_settings=ProxyTable().dict(no_obj=True))
        session.add(user)
        await session.flush()
        for offset in range(count):
            update = UserSubscriptionUpdate(user_id=user.id, user_agent=f"client-{offset}")
            update.created_at = now - timedelta(minutes=offset)
            session.add(update)
        await session.commit()
        return user.id


async def _update_count(session_factory, user_id: int) -> int:  # noqa: F811
    async with session_factory() as session:
        return (
            await session.execute(
                select(func.count(UserSubscriptionUpdate.id)).where(UserSubscriptionUpdate.user_id == user_id)
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_cleanup_keeps_the_clients_limit_from_panel_settings(monkeypatch, session_factory):  # noqa: F811
    monkeypatch.setattr(cleanup_subscription_updates, "GetDB", record_usages.GetDB)
    monkeypatch.setattr(cleanup_subscription_updates, "flush_user_sub_updates", AsyncMock())
    monkeypatch.setenv("USER_SUBSCRIPTION_CLIENTS_LIMIT", "1")
    user_id = await _user_with_updates(session_factory, 6)

    _serve_subscription(monkeypatch, 4)
    await cleanup_subscription_updates.cleanup_user_subscription_updates()
    assert await _update_count(session_factory, user_id) == 4

    _serve_subscription(monkeypatch, 0)
    await cleanup_subscription_updates.cleanup_user_subscription_updates()
    assert await _update_count(session_factory, user_id) == 4

    _serve_subscription(monkeypatch, 2)
    await cleanup_subscription_updates.cleanup_user_subscription_updates()
    assert await _update_count(session_factory, user_id) == 2
