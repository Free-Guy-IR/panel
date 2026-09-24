from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from app.db.models import UserStatus
from app.models.settings import Application
from app.templates import render_template

HOSTILE = [
    "javascript:alert(document.domain)",
    " JavaScript:alert(1)",
    "java\tscript:alert(1)",
    "\x01javascript:alert(1)",
    "javascript://x%0Aalert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
]


class _Attributes(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            self.values.append((tag, name, value or ""))


def _attributes(page: str) -> list[tuple[str, str, str]]:
    parser = _Attributes()
    parser.feed(page)
    return parser.values


def _render(*, announce_url: str = "", apps: list[Application] | None = None, links: list[str] | None = None) -> str:
    user = SimpleNamespace(
        username="u",
        status=UserStatus.active,
        data_limit=None,
        used_traffic=0,
        data_limit_reset_strategy="no_reset",
        expire=None,
        on_hold_expire_duration=None,
        on_hold_timeout=None,
    )
    return render_template(
        "subscription/index.html",
        {
            "user": user,
            "links": links or [],
            "announce": "notice",
            "announce_url": announce_url,
            "apps": apps or [],
            "has_openvpn": False,
            "openvpn_configs": [],
            "l2tp_details": [],
            "configs_hidden_by_hwid": False,
        },
    )


def _app(*, import_url: str = "", icon_url: str = "", download_url: str = "https://example.com/app") -> Application:
    template = Application(
        name="client",
        platform="android",
        icon_url=icon_url,
        download_links=[{"name": "Get", "url": download_url, "language": "en"}],
    )
    return template.model_copy(update={"import_url": import_url})


def _executable(value: str) -> bool:
    cleaned = "".join(ch for ch in value if ord(ch) > 0x20).lower()
    return cleaned.startswith(("javascript:", "data:text", "vbscript:"))


@pytest.mark.parametrize("hostile", HOSTILE)
def test_no_hostile_url_reaches_an_href_src_or_import_target(hostile):
    page = _render(
        announce_url=hostile,
        apps=[_app(import_url=hostile, icon_url=hostile, download_url=hostile)],
    )
    for tag, name, value in _attributes(page):
        if name in ("href", "src", "data-url"):
            assert not _executable(value), f"<{tag} {name}> carries {value!r}"


def test_legitimate_urls_are_still_rendered():
    page = _render(
        announce_url="https://t.me/channel",
        apps=[
            _app(
                import_url="v2rayng://install-sub?url=https://sub.example/sub/TOKEN",
                icon_url="https://example.com/icon.png",
                download_url="https://github.com/2dust/v2rayNG/releases/latest",
            ),
            _app(import_url="happ://add/https://sub.example/sub/TOKEN", icon_url="data:image/png;base64,AAAA"),
        ],
    )
    attributes = _attributes(page)
    assert ("a", "href", "https://t.me/channel") in attributes
    assert ("button", "data-url", "v2rayng://install-sub?url=https://sub.example/sub/TOKEN") in attributes
    assert ("button", "data-url", "happ://add/https://sub.example/sub/TOKEN") in attributes
    assert ("img", "src", "https://example.com/icon.png") in attributes
    assert ("img", "src", "data:image/png;base64,AAAA") in attributes
    assert ("a", "href", "https://github.com/2dust/v2rayNG/releases/latest") in attributes


def test_links_reach_the_copy_handler_as_data_not_code():
    link = "vless://id@1.2.3.4:443?type=tcp#it's');alert(1);//"
    page = _render(links=[link])
    handlers = [value for _, name, value in _attributes(page) if name == "onclick" and value.startswith("copyLink(")]
    assert handlers == ["copyLink(this.dataset.link, this)"]
    assert ("button", "data-link", link) in _attributes(page)
