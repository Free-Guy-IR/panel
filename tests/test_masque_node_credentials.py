import logging

import pytest
from PasarGuardNodeBridge.common.service_pb2 import Masque, Proxy

from app.fork.node_credentials import omit_empty_credentials
from app.models.protocol import ProxyProtocol
from app.node.user import _serialize_or_revoke, _serialize_user_for_node

VLESS_ID = "b5d1e5b2-7a6c-4b55-9a62-2f0b3c3b9f10"


def _masque_pass(proxies) -> str:
    return getattr(proxies.masque, "pass")


def test_an_explicit_empty_masque_section_is_cleared():
    proxies = omit_empty_credentials(Proxy(masque=Masque(**{"pass": ""})))

    assert not proxies.HasField("masque")


def test_a_masque_section_with_a_password_is_kept():
    proxies = omit_empty_credentials(Proxy(masque=Masque(**{"pass": "s3cret"})))

    assert proxies.HasField("masque")
    assert _masque_pass(proxies) == "s3cret"


@pytest.mark.parametrize("stored", [123456, 1.5, True, ["s3cret"], {"pass": "s3cret"}])
def test_a_non_string_masque_password_never_revokes_the_user(stored):
    settings = {"vless": {"id": VLESS_ID}, "masque": {"password": stored}}

    proto = _serialize_or_revoke(7, settings, ["vless-in", "masque-in"], None, None)

    assert list(proto.inbounds) == ["vless-in", "masque-in"]
    assert proto.proxies.vless.id == VLESS_ID
    assert not proto.proxies.HasField("masque")


@pytest.mark.parametrize("settings", [{}, {"masque": {}}, {"masque": {"password": ""}}, {"masque": {"password": None}}])
def test_a_missing_or_empty_masque_password_sends_no_masque_section(settings):
    proto = _serialize_user_for_node(7, settings, ["masque-in"], frozenset({ProxyProtocol.masque}))

    assert not proto.proxies.HasField("masque")


def test_a_stored_masque_password_reaches_the_node():
    proto = _serialize_user_for_node(7, {"masque": {"password": "s3cret"}}, ["masque-in"], frozenset({ProxyProtocol.masque}))

    assert proto.proxies.HasField("masque")
    assert _masque_pass(proto.proxies) == "s3cret"


@pytest.mark.parametrize("stored", ["", 123456, True, ["s3cret"]])
def test_a_dropped_masque_password_is_logged_with_the_user_and_never_the_value(stored, caplog):
    with caplog.at_level(logging.WARNING):
        proto = _serialize_user_for_node(7, {"masque": {"password": stored}}, ["masque-in"], frozenset({ProxyProtocol.masque}))

    assert not proto.proxies.HasField("masque")
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert warnings == ["Dropping an unusable stored MASQUE password for user 7"]


def test_a_usable_or_missing_masque_password_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING):
        _serialize_user_for_node(7, {"masque": {"password": "s3cret"}}, ["masque-in"], frozenset({ProxyProtocol.masque}))
        _serialize_user_for_node(8, {}, ["masque-in"], frozenset({ProxyProtocol.masque}))

    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
