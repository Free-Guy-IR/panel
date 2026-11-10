import pytest

from app.models.settings import ConfigFormat, HeaderPlacement
from app.operation.subscription import SubscriptionOperation

INJECTED = "vmess://injected"

LINE_BREAKING_CHARS = ["\r\n", "\r", "\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]


@pytest.mark.parametrize("breaker", LINE_BREAKING_CHARS)
def test_a_header_value_cannot_add_a_line_to_the_subscription_body(breaker):
    headers = {"X-Note": f"keep{breaker}{INJECTED}"}

    http_headers, body_lines = SubscriptionOperation._split_headers_for_placement(
        headers, HeaderPlacement.body_end, ConfigFormat.links
    )

    assert http_headers == {}
    assert len(body_lines) == 1
    rendered = "\n".join(body_lines)
    assert len(rendered.splitlines()) == 1
    assert rendered.startswith("#")
    assert "keep" in rendered


@pytest.mark.parametrize("breaker", LINE_BREAKING_CHARS)
def test_a_header_name_cannot_add_a_line_to_the_subscription_body(breaker):
    headers = {f"X-No{breaker}te": "value"}

    _, body_lines = SubscriptionOperation._split_headers_for_placement(
        headers, HeaderPlacement.body_end, ConfigFormat.links
    )

    assert len(body_lines) == 1
    assert len("\n".join(body_lines).splitlines()) == 1


def test_a_clean_header_still_becomes_one_comment_line():
    _, body_lines = SubscriptionOperation._split_headers_for_placement(
        {"Profile-Title": "My Shop"}, HeaderPlacement.body_end, ConfigFormat.links
    )

    assert body_lines == ["#Profile-Title: My Shop"]


def test_always_http_headers_are_not_moved_into_the_body():
    headers = {"Content-Disposition": 'attachment; filename="x"', "X-Note": "keep"}

    http_headers, body_lines = SubscriptionOperation._split_headers_for_placement(
        headers, HeaderPlacement.body_end, ConfigFormat.links
    )

    assert http_headers == {"Content-Disposition": 'attachment; filename="x"'}
    assert body_lines == ["#X-Note: keep"]
