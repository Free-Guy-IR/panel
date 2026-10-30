import importlib

import pytest

import app.settings
import config

READER_MODULES = (
    "app.jobs.record_usages",
    "app.jobs.reset_user_data_usage",
    "app.jobs.review_users",
    "app.operation.user",
)


@pytest.mark.parametrize("module_name", READER_MODULES)
def test_flush_fence_and_every_reset_path_read_the_same_settings_object(module_name):
    module = importlib.import_module(module_name)
    assert module.general_settings is app.settings.general_settings


def test_clean_chart_setting_is_no_longer_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("RESET_USER_USAGE_CLEAN_CHART_DATA", "true")
    assert "reset_user_usage_clean_chart_data" not in config.UsageSettings.model_fields
    assert not hasattr(config.UsageSettings(), "reset_user_usage_clean_chart_data")


def test_the_migration_carries_the_old_clean_chart_variable_into_general_usage():
    migration = importlib.import_module("app.db.migrations.versions.b849540d85bb_move_runtime_env_vars_to_settings")
    column, path, _ = migration.ENV_SETTINGS["RESET_USER_USAGE_CLEAN_CHART_DATA"]
    assert (column, path) == ("general", ("usage", "reset_usage_clean_chart_data"))
