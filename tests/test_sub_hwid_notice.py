from typing import ClassVar

from app.operation.subscription import SubscriptionOperation


class _Settings:
    announce_url = ""
    applications: ClassVar[list] = []


def _payload(**over):
    operation = SubscriptionOperation.__new__(SubscriptionOperation)
    kwargs = {
        "user": None,
        "links": [],
        "formatted_announce": "",
        "sub_settings": _Settings(),
        "format_variables": {},
        "is_hwid_enabled": False,
    }
    kwargs.update(over)
    return operation._build_subscription_body_payload(**kwargs)


def test_the_flag_defaults_to_false(monkeypatch):
    monkeypatch.setattr("app.models.user.SubscriptionUserResponse.model_validate", staticmethod(lambda u: u))
    assert _payload()["configs_hidden_by_hwid"] is False


def test_the_flag_is_carried_into_the_template_context(monkeypatch):
    monkeypatch.setattr("app.models.user.SubscriptionUserResponse.model_validate", staticmethod(lambda u: u))
    payload = _payload(configs_hidden_by_hwid=True)
    assert payload["configs_hidden_by_hwid"] is True
    assert payload["links"] == []


def test_a_device_locked_page_carries_no_configs(monkeypatch):
    monkeypatch.setattr("app.models.user.SubscriptionUserResponse.model_validate", staticmethod(lambda u: u))
    payload = _payload(configs_hidden_by_hwid=True, links=[], has_openvpn=False, l2tp_details=[])
    assert payload["configs_hidden_by_hwid"] is True
    assert payload["links"] == []
    assert payload["has_openvpn"] is False
    assert payload["l2tp_details"] == []
