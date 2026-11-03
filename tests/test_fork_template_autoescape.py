from types import SimpleNamespace

import pytest

from app.db.models import DataLimitResetStrategy, UserStatus
from app.templates import env, render_template

HOSTILE = "a'b\"c<script>alert(1)</script>&d"
HOSTILE_ESCAPED = "a&#39;b&#34;c&lt;script&gt;alert(1)&lt;/script&gt;&amp;d"
NORMAL_LINK = "vless://id@host.example:443?type=ws&path=%2Fws#Tom & Jerry"
NORMAL_LINK_ESCAPED = "vless://id@host.example:443?type=ws&amp;path=%2Fws#Tom &amp; Jerry"
DOUBLE_ESCAPES = ("&amp;amp;", "&amp;lt;", "&amp;gt;", "&amp;#39;", "&amp;#34;", "&amp;quot;")


def _user(username: str):
    return SimpleNamespace(
        username=username,
        status=UserStatus.active,
        data_limit=0,
        used_traffic=0,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        on_hold_expire_duration=None,
        expire=None,
        on_hold_timeout=None,
    )


def _app(name: str, import_url: str):
    return SimpleNamespace(
        icon_url="https://icons.example/app.png",
        name=name,
        recommended=False,
        platform=None,
        description={"en": name},
        download_links=[],
        import_url=import_url,
    )


def _render(value: str, link: str, import_url: str) -> str:
    return render_template(
        "subscription/index.html",
        {
            "user": _user(value),
            "links": [link],
            "announce": value,
            "announce_url": "https://news.example/?a=1&b=2",
            "apps": [_app(value, import_url)],
        },
    )


def test_page_templates_render_with_autoescape_on():
    assert env.autoescape is True or env.autoescape("subscription/index.html") is True
    assert env.get_template("subscription/index.html").render(
        user=_user("u"), links=[], announce="", announce_url="", apps=[]
    )


def test_fork_subscription_page_escapes_a_hostile_value_exactly_once():
    html = _render(HOSTILE, HOSTILE, "happ://add/" + HOSTILE)

    assert "<script>alert(1)</script>" not in html
    assert HOSTILE_ESCAPED in html
    assert f'data-link="{HOSTILE_ESCAPED}"' in html
    assert f'data-url="happ://add/{HOSTILE_ESCAPED}"' in html
    for double in DOUBLE_ESCAPES:
        assert double not in html


@pytest.mark.parametrize("import_url", ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>"])
def test_fork_subscription_page_drops_a_script_import_url(import_url):
    html = _render("normal", NORMAL_LINK, import_url)

    assert "javascript:alert" not in html
    assert "data:text/html" not in html


def test_fork_subscription_page_keeps_a_normal_value_readable():
    html = _render("Tom & Jerry", NORMAL_LINK, "happ://add/https://sub.example/x?a=1&b=2")

    assert "Tom &amp; Jerry" in html
    assert f'data-link="{NORMAL_LINK_ESCAPED}"' in html
    assert 'data-url="happ://add/https://sub.example/x?a=1&amp;b=2"' in html
    assert 'href="https://news.example/?a=1&amp;b=2"' in html
    for double in DOUBLE_ESCAPES:
        assert double not in html
