import pytest
from pydantic import ValidationError

from app.models.admin import AdminContactInfo, AdminCreate, AdminModify
from app.models.settings import SettingsSchemaInput, Subscription

LIMIT = 100


def _variables(count: int) -> list[dict]:
    return [{"key": f"V{index}", "value": "{USERNAME}"} for index in range(count)]


def _inputs(count: int) -> list:
    return [
        lambda: SettingsSchemaInput.model_validate(
            {"subscription": {"rules": [], "custom_variables": _variables(count)}}
        ),
        lambda: SettingsSchemaInput.model_validate({"general": {"custom_variables": _variables(count)}}),
        lambda: AdminModify.model_validate({"custom_variables": _variables(count)}),
        lambda: AdminCreate.model_validate(
            {"username": "reseller", "password": "Aa12!Bb34@cdEF", "role_id": 3, "custom_variables": _variables(count)}
        ),
    ]


@pytest.mark.parametrize("path", range(4))
def test_saving_more_than_the_limit_of_custom_variables_is_refused(path):
    with pytest.raises(ValidationError):
        _inputs(LIMIT + 1)[path]()


@pytest.mark.parametrize("path", range(4))
def test_saving_up_to_the_limit_of_custom_variables_still_works(path):
    _inputs(LIMIT)[path]()


def test_a_stored_list_longer_than_the_limit_still_loads():
    from app.fork.format_template import MAX_CUSTOM_VARIABLES

    assert MAX_CUSTOM_VARIABLES == LIMIT
    assert len(Subscription.model_validate({"rules": [], "custom_variables": _variables(150)}).custom_variables) == 150
    assert (
        len(AdminContactInfo.model_validate({"username": "a", "custom_variables": _variables(150)}).custom_variables)
        == 150
    )


def test_only_the_first_hundred_stored_custom_variables_of_each_list_are_applied():
    from types import SimpleNamespace

    from app.models.settings import CustomVariable
    from app.subscription.share import get_effective_custom_variables

    stored_settings = [CustomVariable(key=f"S{index}", value="{USERNAME}") for index in range(5000)]
    stored_admin = [CustomVariable(key=f"A{index}", value="{USERNAME}") for index in range(5000)]
    user = SimpleNamespace(admin=SimpleNamespace(custom_variables=stored_admin))

    applied = get_effective_custom_variables(user, stored_settings)

    assert [variable.key for variable in applied] == [f"S{index}" for index in range(LIMIT)] + [
        f"A{index}" for index in range(LIMIT)
    ]


def test_a_legacy_admin_with_thousands_of_custom_variables_formats_at_most_the_limit(monkeypatch):
    from types import SimpleNamespace

    from app.models.settings import CustomVariable
    from app.models.user import UserStatus
    from app.operation.subscription import SubscriptionOperation
    from app.subscription import share

    formatted = []
    bounded = share.format_bounded

    def counting(template, variables):
        formatted.append(template)
        return bounded(template, variables)

    monkeypatch.setattr(share, "format_bounded", counting)
    variables = [CustomVariable(key=f"V{index}", value="{USERNAME:256}" * 16) for index in range(5000)]
    admin = SimpleNamespace(username="reseller", profile_title="{V0}", custom_variables=variables, support_url=None)
    user = SimpleNamespace(
        username="alice",
        status=UserStatus.active,
        expire=None,
        on_hold_expire_duration=None,
        admin=admin,
        data_limit=None,
        used_traffic=0,
    )
    settings = SimpleNamespace(
        custom_variables=[], profile_title="", announce="", announce_url="", support_url="", update_interval=12
    )

    SubscriptionOperation.create_response_headers(user, "https://sub.example.com/sub/x", settings)

    assert len(formatted) <= 3 * LIMIT
