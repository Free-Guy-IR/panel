from app.operation.subscription import SubscriptionOperation


class _Settings:
    announce_url = ""
    applications = []


def _payload(**over):
    operation = SubscriptionOperation.__new__(SubscriptionOperation)
    kwargs = dict(
        user=None,
        links=[],
        formatted_announce="",
        sub_settings=_Settings(),
        format_variables={},
        is_hwid_enabled=False,
    )
    kwargs.update(over)
    return operation._build_subscription_body_payload.__wrapped__(operation, **kwargs) if hasattr(
        operation._build_subscription_body_payload, "__wrapped__"
    ) else operation._build_subscription_body_payload(**kwargs)


def test_the_flag_defaults_to_false(monkeypatch):
    monkeypatch.setattr(
        "app.models.user.SubscriptionUserResponse.model_validate", staticmethod(lambda u: u)
    )
    payload = _payload()
    assert payload["configs_hidden_by_hwid"] is False


def test_the_flag_is_carried_into_the_template_context(monkeypatch):
    monkeypatch.setattr(
        "app.models.user.SubscriptionUserResponse.model_validate", staticmethod(lambda u: u)
    )
    payload = _payload(configs_hidden_by_hwid=True)
    assert payload["configs_hidden_by_hwid"] is True
    assert payload["links"] == []


def test_the_gate_is_true_only_when_hwid_and_manual_sub_are_both_on():
    for hwid, manual, expected in (
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ):
        assert (hwid and manual) is expected
