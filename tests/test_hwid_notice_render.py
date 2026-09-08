import pytest

from app.models.settings import HWIDSettings
from app.operation.subscription import SubscriptionOperation

TEMPLATE = """
{% if configs_hidden_by_hwid %}<div class="hwidbox">locked</div>{% endif %}
{% if not configs_hidden_by_hwid %}<button id="cfgBtn">configs</button>{% endif %}
{% for l in links %}<a class="cfg">{{ l }}</a>{% endfor %}
"""


def _Global(*, enabled=True, forced=True, manual=True, fallback_limit=None):
    return HWIDSettings(
        enabled=enabled,
        forced=forced,
        require_hwid_for_manual_sub=manual,
        fallback_limit=fallback_limit,
    )


def _hwid_enabled(user_limit, *, glob):
    return SubscriptionOperation.is_hwid_enabled(glob, glob, user_limit)


def _render(configs_hidden, links):
    from jinja2 import Environment

    return Environment(autoescape=True).from_string(TEMPLATE).render(
        configs_hidden_by_hwid=configs_hidden, links=links
    )


@pytest.mark.parametrize(
    ("user_limit", "forced", "expected"),
    [
        (None, True, True),
        (3, True, True),
        (3, False, True),
        (0, True, False),
        (0, False, False),
        (None, False, False),
    ],
)
def test_who_the_policy_treats_as_device_locked(user_limit, forced, expected):
    assert _hwid_enabled(user_limit, glob=_Global(forced=forced)) is expected


def test_a_disabled_policy_locks_nobody():
    assert _hwid_enabled(5, glob=_Global(enabled=False)) is False


def test_an_enforced_user_sees_the_notice_and_no_configs():
    hidden = _hwid_enabled(None, glob=_Global()) and _Global().require_hwid_for_manual_sub
    html = _render(hidden, [])
    assert 'class="hwidbox"' in html
    assert 'id="cfgBtn"' not in html
    assert 'class="cfg"' not in html


def test_a_user_who_opted_out_with_a_zero_limit_keeps_their_configs():
    hidden = _hwid_enabled(0, glob=_Global()) and _Global().require_hwid_for_manual_sub
    html = _render(hidden, ["vless://a", "vmess://b"])
    assert 'class="hwidbox"' not in html
    assert 'id="cfgBtn"' in html
    assert html.count('class="cfg"') == 2


def test_a_user_outside_the_policy_groups_keeps_their_configs():
    html = _render(False, ["vless://a"])
    assert 'class="hwidbox"' not in html
    assert 'id="cfgBtn"' in html
    assert 'class="cfg"' in html


def test_manual_sub_toggle_off_means_the_page_is_never_stripped():
    glob = _Global(manual=False)
    hidden = _hwid_enabled(None, glob=glob) and glob.require_hwid_for_manual_sub
    assert hidden is False
    html = _render(hidden, ["vless://a"])
    assert 'class="hwidbox"' not in html
    assert 'id="cfgBtn"' in html


def test_the_coverage_check_reads_apply_to_group_ids_not_group_ids():
    import inspect

    from app.utils.hwid import hwid_covers_user

    source = inspect.getsource(hwid_covers_user)
    assert "apply_to_group_ids" in source
