import pytest

from app.models.settings import Subscription, SubscriptionInput


@pytest.mark.parametrize("model", [Subscription, SubscriptionInput])
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://sub.example.com/", "https://sub.example.com"),
        ("/https://sub.example.com//", "https://sub.example.com"),
        ("https://sub.example.com/path/", "https://sub.example.com/path"),
        ("https://sub.example.com", "https://sub.example.com"),
        ("/", ""),
        ("", ""),
    ],
)
def test_subscription_url_prefix_loses_surrounding_slashes(model, raw, expected):
    assert model(url_prefix=raw, rules=[]).url_prefix == expected
