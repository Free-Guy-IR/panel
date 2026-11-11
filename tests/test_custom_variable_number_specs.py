import pytest
from pydantic import ValidationError

from app.models.settings import CustomVariableInput


@pytest.mark.parametrize(
    "value",
    [
        "{DAYS_LEFT:,}",
        "{DAYS_LEFT:03d}",
        "{DAYS_LEFT:_}",
        "{USAGE_PERCENTAGE:.1f}",
        "{USAGE_PERCENTAGE:5.1%}",
        "{USERNAME:d}",
        "{DAYS_LEFT:.2}",
        "{DAYS_LEFT:s}",
        "{USAGE_PERCENTAGE:s}",
    ],
)
def test_a_format_spec_that_does_not_fit_every_value_of_its_variable_is_refused(value):
    with pytest.raises(ValidationError):
        CustomVariableInput(key="PROMO", value=value)


@pytest.mark.parametrize(
    "value",
    ["{DAYS_LEFT}", "{DAYS_LEFT:>4}", "{DAYS_LEFT!s:>3}", "{USAGE_PERCENTAGE:.3}", "{USERNAME:>10}", "{SHOP:^9}"],
)
def test_a_format_spec_every_value_of_its_variable_fits_saves(value):
    assert CustomVariableInput(key="PROMO", value=value).value == value


@pytest.mark.parametrize("value", ["{USERNAME:q}", "{DAYS_LEFT:,s}", "{USERNAME!r:d}", "{SHOP:.2q}"])
def test_a_format_no_value_could_take_is_still_refused(value):
    with pytest.raises(ValidationError):
        CustomVariableInput(key="PROMO", value=value)
