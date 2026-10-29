from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models.settings import Application, CustomVariable, CustomVariableInput, Subscription as SubSettings
from app.operation.subscription import SubscriptionOperation
from app.subscription.share import apply_custom_format_variables

OVERSIZED = ["{USERNAME:>300}", "{USERNAME:.300}", "{USERNAME:>300}{", "{USERNAME:>{PAD}}", "{USERNAME}{"]
UNFILLABLE = ["{USERNAME.missing}", "{USERNAME[9]}", "{0}"]


class _CountingField:
    def __init__(self):
        self.formatted = 0

    def __format__(self, spec):
        self.formatted += 1
        return "x"


def _rendered(template: str, field: _CountingField) -> list[str]:
    variables = {"USERNAME": field, "PAD": "300", "url": field}
    settings = SubSettings.model_construct(profile_title=template, announce=template, announce_url=template)
    application = Application.model_construct(import_url=template, show_when_hwid_enabled=False)
    return [
        apply_custom_format_variables(dict(variables), [{"key": "CUSTOM", "value": template}])["CUSTOM"],
        SubscriptionOperation._format_profile_title(SimpleNamespace(admin=None), variables, settings),
        SubscriptionOperation._format_announce(settings, variables),
        SubscriptionOperation._format_announce_url(settings, variables),
        SubscriptionOperation._stringify_rule_header_value(template, variables),
        SubscriptionOperation._make_apps_import_urls(None, [application], variables, is_hwid_enabled=False)[
            0
        ].import_url,
    ]


@pytest.mark.parametrize("template", OVERSIZED)
def test_an_oversized_or_unparseable_template_is_served_raw_and_never_formatted(template):
    field = _CountingField()

    assert _rendered(template, field) == [template] * 6
    assert field.formatted == 0


@pytest.mark.parametrize("template", UNFILLABLE)
def test_a_template_the_variables_cannot_fill_is_served_raw(template):
    assert _rendered(template, _CountingField()) == [template] * 6


@pytest.mark.parametrize("value", ["{USERNAME:>300}", "{USERNAME:>300}{", "{USERNAME.missing}", "{USERNAME[0]}"])
def test_a_custom_variable_that_would_pad_hugely_or_read_attributes_is_refused_on_save(value):
    with pytest.raises(ValidationError):
        CustomVariableInput(key="CUSTOM", value=value)


def test_ordinary_templates_still_render():
    field = _CountingField()
    variables = {"USERNAME": field}
    settings = SubSettings.model_construct(announce="hi {USERNAME:>5} {{x}}")

    assert SubscriptionOperation._format_announce(settings, variables) == "hi x {x}"
    assert CustomVariableInput(key="CUSTOM", value="{USERNAME:>256}").value == "{USERNAME:>256}"
    assert CustomVariable(key="CUSTOM", value="{USERNAME:>300}").value == "{USERNAME:>300}"
