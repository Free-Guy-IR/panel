from types import SimpleNamespace

from app.models.user import UserStatus
from app.operation.subscription import SubscriptionOperation


def _user(support_url: str = ""):
    admin = SimpleNamespace(support_url=support_url, profile_title=None, custom_variables=[], username="admin")
    return SimpleNamespace(
        username="subuser",
        status=UserStatus.active,
        expire=None,
        on_hold_expire_duration=None,
        data_limit=None,
        used_traffic=0,
        admin=admin,
    )


def _settings(**overrides):
    defaults = {
        "profile_title": "Subscription",
        "custom_variables": [],
        "announce": "",
        "announce_url": "",
        "support_url": "",
        "update_interval": 12,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_encode_url_header_converts_idn_host_to_punycode():
    encoded = SubscriptionOperation._encode_url_header("https://مثال.ir/info")
    assert encoded == "https://xn--mgbh0fb.ir/info"
    encoded.encode("latin-1")


def test_encode_url_header_percent_encodes_non_ascii_path():
    encoded = SubscriptionOperation._encode_url_header("https://example.ir/مسیر")
    assert encoded.startswith("https://example.ir/%")
    encoded.encode("latin-1")


def test_encode_url_header_leaves_ascii_urls_untouched():
    url = "https://example.com/a?b=c%20d#frag"
    assert SubscriptionOperation._encode_url_header(url) == url


def test_encode_url_header_passes_through_empty_values():
    assert SubscriptionOperation._encode_url_header("") == ""
    assert SubscriptionOperation._encode_url_header(None) is None


def test_response_headers_with_non_ascii_urls_survive_sanitize():
    headers = SubscriptionOperation.create_response_headers(
        _user(support_url="https://پشتیبانی.example.ir/راهنما"),
        "https://panel.example.com/sub/token",
        _settings(announce_url="https://مثال.ir/اطلاعات"),
    )
    cleaned = SubscriptionOperation.sanitize_response_headers(headers)
    assert cleaned["support-url"].startswith("https://xn--")
    assert cleaned["announce-url"].startswith("https://xn--")


def test_info_response_headers_with_non_ascii_urls_survive_sanitize():
    headers = SubscriptionOperation.create_info_response_headers(
        _user(support_url="https://پشتیبانی.example.ir"),
        _settings(announce_url="https://مثال.ir"),
    )
    cleaned = SubscriptionOperation.sanitize_response_headers(headers)
    assert cleaned["support-url"].encode("latin-1")
    assert cleaned["announce-url"].encode("latin-1")
