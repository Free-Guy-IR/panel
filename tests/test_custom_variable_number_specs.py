import pytest
from pydantic import ValidationError

from app.models.settings import CustomVariableInput


@pytest.mark.parametrize(
    "value",
    ["{DAYS_LEFT:,}", "{DAYS_LEFT:03d}", "{USAGE_PERCENTAGE:.1f}", "{USAGE_PERCENTAGE:5.1%}", "{DAYS_LEFT:_}"],
)
def test_a_number_format_for_a_number_variable_saves(value):
    assert CustomVariableInput(key="PROMO", value=value).value == value


@pytest.mark.parametrize("value", ["{USERNAME:q}", "{DAYS_LEFT:,s}", "{USERNAME!r:d}", "{SHOP:.2q}"])
def test_a_format_no_value_could_take_is_still_refused(value):
    with pytest.raises(ValidationError):
        CustomVariableInput(key="PROMO", value=value)
