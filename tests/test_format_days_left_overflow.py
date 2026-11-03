import pytest

from app.subscription.share import _format_dynamic_value, apply_custom_format_variables

HUGE_DAYS_LEFT = 0x110000 + 1


@pytest.mark.parametrize("value", ["{DAYS_LEFT:c}", ["{DAYS_LEFT:c}"], {"remark": "{DAYS_LEFT:c}"}])
def test_a_char_spec_on_a_huge_days_left_keeps_the_value_raw(value):
    assert _format_dynamic_value(value, {"DAYS_LEFT": HUGE_DAYS_LEFT}) == value


def test_a_custom_variable_with_a_char_spec_on_a_huge_days_left_keeps_its_raw_value():
    variables = apply_custom_format_variables(
        {"DAYS_LEFT": HUGE_DAYS_LEFT}, [{"key": "LEFT", "value": "{DAYS_LEFT:c}"}]
    )
    assert variables["LEFT"] == "{DAYS_LEFT:c}"
