import random
import tracemalloc
from types import SimpleNamespace

import pytest

from app.models.settings import CustomVariable
from app.models.user import UserStatus
from app.operation.subscription import SubscriptionOperation
from app.subscription.share import _format_dynamic_value

GROWTH = 4096
VARIABLES = {"USERNAME": "alice", "DAYS_LEFT": 12, "USAGE": 15.25, "LONG": "x" * 500, "EMPTY": ""}


def _headers(profile_title: str, custom_variables: list[CustomVariable]) -> tuple[dict, int]:
    admin = SimpleNamespace(
        username="reseller", profile_title=profile_title, custom_variables=custom_variables, support_url=None
    )
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
        custom_variables=[],
        profile_title="Subscription",
        announce="",
        announce_url="",
        support_url="https://t.me/",
        update_interval=12,
    )
    tracemalloc.start()
    try:
        headers = SubscriptionOperation.create_response_headers(user, "https://sub.example.com/sub/x", settings)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return headers, peak


@pytest.mark.parametrize(
    "first", ["{USERNAME:256}" * 36, "{DAYS_LEFT.__class__.__dict__}" * 16], ids=["widths", "attributes"]
)
def test_chained_custom_variables_and_profile_title_stay_within_a_fixed_size(first):
    variables = [CustomVariable(key="A", value=first)]
    variables += [CustomVariable(key=f"B{index}", value="{PROFILE_TITLE}" * 34) for index in range(3)]

    headers, peak = _headers("{A}" * 170, variables)

    assert len(headers["profile-title"]) <= 4 * (len("{A}" * 170) + GROWTH)
    assert peak < 2_000_000


def test_a_stored_list_of_many_custom_variables_at_the_growth_limit_stays_small():
    variables = [CustomVariable(key=f"V{index}", value="{USERNAME:256}" * 16) for index in range(300)]

    headers, peak = _headers("{V0}{V1}", variables)

    assert len(headers["profile-title"]) <= len("{V0}{V1}") + GROWTH
    assert peak < 10_000_000


@pytest.mark.parametrize(
    "template", ["{USERNAME.__class__}", "{USERNAME[0]}", "{EMPTY.__class__.__dict__}", "{USERNAME!x}"]
)
def test_a_field_that_reads_an_attribute_or_an_index_is_served_raw(template):
    assert _format_dynamic_value(template, dict(VARIABLES)) == template
    assert _format_dynamic_value([template], dict(VARIABLES)) == [template]
    assert _format_dynamic_value({"key": template}, dict(VARIABLES)) == {"key": template}


def test_a_result_more_than_the_growth_limit_past_its_template_is_served_raw():
    from app.fork.format_template import MAX_FORMAT_GROWTH

    assert MAX_FORMAT_GROWTH == GROWTH
    variables = dict(VARIABLES)
    assert _format_dynamic_value("{LONG}" * 8, variables) == "x" * 4000
    assert _format_dynamic_value("{LONG}" * 9, variables) == "{LONG}" * 9
    at_the_limit = "{LONG}" * 8 + "{USERNAME:158}"
    assert len(at_the_limit) + GROWTH == 4000 + 158
    assert _format_dynamic_value(at_the_limit, variables) == "x" * 4000 + "alice".ljust(158)
    one_past = "{LONG}" * 8 + "{USERNAME:159}"
    assert _format_dynamic_value(one_past, variables) == one_past


def _random_template(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randint(0, 6)):
        kind = rng.random()
        if kind < 0.3:
            parts.append(rng.choice(["abc ", "{{", "}}", " 50% ", "x-y", "متن"]))
            continue
        name = rng.choice(["USERNAME", "DAYS_LEFT", "USAGE", "EMPTY"])
        conversion = rng.choice(["", "", "!s", "!r", "!a"])
        align = rng.choice(["", "<", ">", "^"])
        width = rng.choice(["", "0", "7", "20", "256"])
        precision = rng.choice(["", ".0", ".3"])
        spec = f"{align}{width}{precision}"
        parts.append("{" + name + conversion + (":" + spec if spec else "") + "}")
    return "".join(parts)


def test_an_ordinary_template_renders_exactly_as_format_map_does():
    rng = random.Random(1053)
    compared = 0
    for _ in range(3000):
        template = _random_template(rng)
        try:
            expected = template.format_map(VARIABLES)
        except ValueError, TypeError:
            assert _format_dynamic_value(template, dict(VARIABLES)) == template
            continue
        assert _format_dynamic_value(template, dict(VARIABLES)) == expected, template
        compared += 1
    assert compared > 1000
