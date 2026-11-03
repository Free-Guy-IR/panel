import pytest

from app.fork.format_template import check_template_value, format_bounded
from app.subscription.share import _format_dynamic_value

CHAR_TEMPLATES = ["{DAYS_LEFT:c}", "{DAYS_LEFT:>5c}", "{DAYS_LEFT:*^9c}"]


@pytest.mark.parametrize("template", CHAR_TEMPLATES)
def test_a_character_presentation_type_is_refused_on_save(template):
    with pytest.raises(ValueError):
        check_template_value(template)


@pytest.mark.parametrize("days_left", [1_131_501, 0xD800, 0x110000])
@pytest.mark.parametrize("template", CHAR_TEMPLATES)
def test_a_stored_character_presentation_type_renders_raw_and_never_raises(template, days_left):
    assert _format_dynamic_value(template, {"DAYS_LEFT": days_left}) == template
    assert format_bounded(template, {"DAYS_LEFT": days_left}) == template


def test_a_fill_character_c_still_formats():
    assert check_template_value("{USERNAME:c<8}") == "{USERNAME:c<8}"
    assert format_bounded("{USERNAME:c<8}", {"USERNAME": "al"}) == "alcccccc"
