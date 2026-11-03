import pytest
from pydantic import ValidationError

from app.models.host import validate_format_variables
from app.models.settings import CustomVariableInput

UNBALANCED = ["/legacy{", "edge}x", "{USERNAME", "a { b", "Welcome {SHOP} :}", "{USERNAME:>300}{"]


@pytest.mark.parametrize("value", UNBALANCED)
def test_a_host_field_with_a_stray_brace_gets_the_brace_message(value):
    with pytest.raises(ValueError) as refused:
        validate_format_variables(value)

    assert str(refused.value) == f'Invalid formatting variables in "{value}"'


@pytest.mark.parametrize("value", UNBALANCED)
def test_a_custom_variable_with_a_stray_brace_gets_the_brace_message(value):
    with pytest.raises(ValidationError) as refused:
        CustomVariableInput(key="PROMO", value=value)

    message = str(refused.value)
    assert "width and precision" not in message
    assert "{{" in message


@pytest.mark.parametrize("value", ["{USERNAME:>300}", "{USERNAME:.1000}"])
def test_an_oversized_width_still_gets_the_width_message(value):
    with pytest.raises(ValueError) as host_refused:
        validate_format_variables(value)
    with pytest.raises(ValidationError) as variable_refused:
        CustomVariableInput(key="PROMO", value=value)

    assert "width and precision must be numbers up to 256" in str(host_refused.value)
    assert "width and precision must be numbers up to 256" in str(variable_refused.value)
