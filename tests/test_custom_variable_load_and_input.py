import tracemalloc

import pytest

from app.fork.operation.custom_variables import custom_variable_refusal
from app.models.admin import AdminContactInfo, AdminCreate, AdminModify
from app.models.settings import SettingsSchema, SettingsSchemaInput, Subscription

STORED_BEFORE_THE_STRICT_CHECK = [
    "{USERNAME:>300}",
    "{USERNAME:>{DAYS_LEFT}}",
    "Welcome {SHOP} :}",
    "{SHOP} {",
    "{DATA_LEFT:^1000}",
    "{USERNAME.__class__.__dict__}",
    "{NOPE}{USERNAME[0]}",
]
REFUSED_ON_SAVE = [
    *STORED_BEFORE_THE_STRICT_CHECK,
    "{NOPE}{USERNAME.x}",
    "{NOPE}{0}",
    "{NOPE}{}",
    "{NOPE}{USERNAME:>300}",
    "{DAYS_LEFT.__class__.__dict__}" * 16,
]
SAVED_AS_WRITTEN = ["{USERNAME}.example.com", "{SHOP} 50% {{off}}", "{USERNAME:>20}", "{NOPE}", "plain text"]


def _variables(value: str) -> list[dict]:
    return [{"key": "PROMO", "value": value}]


def _settings_input(section: str, value: str) -> dict:
    if section == "general":
        return {"general": {"custom_variables": _variables(value)}}
    return {"subscription": {"rules": [], "custom_variables": _variables(value)}}


def _admin_input(model, value: str):
    extra = {"username": "reseller", "password": "Aa12!Bb34@cdEF", "role_id": 3} if model is AdminCreate else {}
    return model.model_validate({**extra, "custom_variables": _variables(value)})


@pytest.mark.parametrize("value", STORED_BEFORE_THE_STRICT_CHECK)
def test_a_stored_custom_variable_loads_whatever_it_holds(value):
    subscription = Subscription.model_validate({"rules": [], "custom_variables": _variables(value)})
    admin = AdminContactInfo.model_validate({"username": "reseller", "custom_variables": _variables(value)})
    settings = SettingsSchema.model_validate(
        {"subscription": {"rules": [], "custom_variables": _variables(value)}, "general": {}}
    )

    assert subscription.custom_variables[0].value == value
    assert admin.custom_variables[0].value == value
    assert settings.subscription.custom_variables[0].value == value


def test_loading_a_stored_custom_variable_never_formats_it():
    tracemalloc.start()
    try:
        Subscription.model_validate({"rules": [], "custom_variables": _variables("{USERNAME:>20000000}")})
        AdminContactInfo.model_validate({"username": "reseller", "custom_variables": _variables("{SHOP:>20000000}")})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 5_000_000


def _settings_variables(section: str, value: str):
    return getattr(SettingsSchemaInput.model_validate(_settings_input(section, value)), section).custom_variables


@pytest.mark.parametrize("value", REFUSED_ON_SAVE)
@pytest.mark.parametrize("section", ["subscription", "general"])
def test_saving_settings_refuses_a_new_custom_variable_the_subscription_cannot_format_as_written(section, value):
    refusal = custom_variable_refusal(_settings_variables(section, value), [])
    assert refusal.startswith('Custom variable "PROMO": Invalid formatting variables')


@pytest.mark.parametrize("value", REFUSED_ON_SAVE)
@pytest.mark.parametrize("model", [AdminModify, AdminCreate])
def test_saving_an_admin_refuses_a_new_custom_variable_the_subscription_cannot_format_as_written(model, value):
    refusal = custom_variable_refusal(_admin_input(model, value).custom_variables, [])
    assert refusal.startswith('Custom variable "PROMO": Invalid formatting variables')


@pytest.mark.parametrize("value", STORED_BEFORE_THE_STRICT_CHECK)
@pytest.mark.parametrize("section", ["subscription", "general"])
def test_resending_a_stored_custom_variable_unchanged_is_never_refused(section, value):
    assert custom_variable_refusal(_settings_variables(section, value), _variables(value)) is None
    assert custom_variable_refusal(_admin_input(AdminModify, value).custom_variables, _variables(value)) is None


@pytest.mark.parametrize("value", SAVED_AS_WRITTEN)
def test_ordinary_custom_variables_still_save(value):
    assert (
        SettingsSchemaInput.model_validate(_settings_input("subscription", value))
        .subscription.custom_variables[0]
        .value
        == value
    )
    assert (
        SettingsSchemaInput.model_validate(_settings_input("general", value)).general.custom_variables[0].value == value
    )
    assert _admin_input(AdminModify, value).custom_variables[0].value == value
    assert _admin_input(AdminCreate, value).custom_variables[0].value == value
    assert custom_variable_refusal(_admin_input(AdminCreate, value).custom_variables, []) is None
