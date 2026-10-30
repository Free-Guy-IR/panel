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
