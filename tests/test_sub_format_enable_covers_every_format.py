from app.models.settings import ConfigFormat, SubFormatEnable


def test_every_requestable_format_has_an_enable_flag():
    flags = SubFormatEnable()
    for fmt in ConfigFormat:
        if fmt is ConfigFormat.block:
            continue
        assert hasattr(flags, fmt.value), f"SubFormatEnable lacks a flag for ConfigFormat.{fmt.value}"
        assert getattr(flags, fmt.value) is True


def test_l2tp_flag_round_trips_through_stored_settings():
    stored = SubFormatEnable.model_validate({"links": True, "openvpn": False})
    assert stored.l2tp is True
    assert stored.openvpn is False
    assert SubFormatEnable.model_validate({"l2tp": False}).l2tp is False
