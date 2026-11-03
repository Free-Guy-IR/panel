import importlib

import pytest

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
    assert module.usage_settings is config.usage_settings


@pytest.mark.parametrize("raw, expected", [("true", True), ("false", False), ("1", True), ("0", False)])
def test_clean_chart_setting_comes_from_one_environment_variable(monkeypatch, raw, expected):
    monkeypatch.setenv("RESET_USER_USAGE_CLEAN_CHART_DATA", raw)
    assert config.UsageSettings().reset_user_usage_clean_chart_data is expected


def test_flush_fence_follows_the_shared_setting(monkeypatch):
    record_usages = importlib.import_module("app.jobs.record_usages")
    reset_job = importlib.import_module("app.jobs.reset_user_data_usage")
    for value in (True, False):
        monkeypatch.setattr(config.usage_settings, "reset_user_usage_clean_chart_data", value)
        assert record_usages.usage_settings.reset_user_usage_clean_chart_data is value
        assert reset_job.usage_settings.reset_user_usage_clean_chart_data is value
