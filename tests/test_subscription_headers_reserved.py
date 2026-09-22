from types import SimpleNamespace

from app.models.user import UserStatus
from app.operation.subscription import SubscriptionOperation

FORGED_USERINFO = "upload=0; download=0; total=0; expire=0"


def _user():
    admin = SimpleNamespace(support_url="", profile_title=None, custom_variables=[], username="admin")
    return SimpleNamespace(
        username="subuser",
        status=UserStatus.active,
        expire=None,
        on_hold_expire_duration=None,
        data_limit=1024,
        used_traffic=512,
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
        "response_headers": {},
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_subscription_settings_cannot_set_subscription_userinfo():
    headers = SubscriptionOperation._format_subscription_response_headers(
        _settings(response_headers={"subscription-userinfo": FORGED_USERINFO}), {}
    )

    assert headers == {}


def test_reserved_header_match_is_case_insensitive():
    headers = SubscriptionOperation._format_subscription_response_headers(
        _settings(response_headers={"Subscription-UserInfo": FORGED_USERINFO, " subscription-userinfo ": FORGED_USERINFO}),
        {},
    )

    assert headers == {}


def test_rule_response_headers_cannot_set_subscription_userinfo():
    rule = SimpleNamespace(response_headers={"subscription-userinfo": FORGED_USERINFO, "x-vendor": "keep"})

    headers = SubscriptionOperation._format_rule_response_headers(rule, {})

    assert headers == {"x-vendor": "keep"}


def test_unreserved_headers_are_still_settable():
    headers = SubscriptionOperation._format_subscription_response_headers(
        _settings(response_headers={"profile-title": "Mine", "announce": "hello", "x-custom": "1"}), {}
    )

    assert headers["x-custom"] == "1"
    assert "profile-title" in headers
    assert "announce" in headers


def test_merged_headers_keep_the_computed_traffic_figure():
    user = _user()
    sub_settings = _settings(response_headers={"subscription-userinfo": FORGED_USERINFO})

    headers = SubscriptionOperation.create_response_headers(user, "https://panel.example.com/sub/token", sub_settings)
    headers.update(SubscriptionOperation._format_subscription_response_headers(sub_settings, {}))
    headers = SubscriptionOperation.sanitize_response_headers(headers)

    assert headers["subscription-userinfo"] == "upload=0; download=512; total=1024; expire=0"
