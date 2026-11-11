import logging

from fastapi import status
from sqlalchemy.exc import OperationalError

from app.fork.proxy_secrets.locks import LOCK_UNAVAILABLE_DETAIL, NamedLockMissing
from app.utils.crypto import generate_wireguard_keypair
from tests.api import client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_user,
    delete_core,
    delete_group,
    delete_user,
    unique_name,
)


class _LockWaitTimeout(Exception):
    def __init__(self):
        super().__init__(1205, "Lock wait timeout exceeded")
        self.sqlstate = None


def _wg_config(interface_name, address):
    private_key, _ = generate_wireguard_keypair()
    return {"interface_name": interface_name, "private_key": private_key, "listen_port": 51820, "address": address}


def _wg_core(access_token, interface_name, address):
    return create_core(
        access_token,
        name=unique_name("wg_lock_core"),
        config=_wg_config(interface_name, address),
        type="wg",
        fallbacks=[],
    )


def _stored_core(access_token, core_id):
    return client.get(f"/api/core/{core_id}", headers=auth_headers(access_token))


def _put_core(access_token, core, address):
    return client.put(
        f"/api/core/{core['id']}",
        headers=auth_headers(access_token),
        params={"restart_nodes": False},
        json={
            "name": core["name"],
            "type": "wg",
            "config": {**core["config"], "address": address},
            "exclude_inbound_tags": [],
            "fallbacks_inbound_tags": [],
        },
    )


def _lock_is_busy(patch):
    from app.fork.operation import core_extras

    async def busy(db):
        raise NamedLockMissing("the 'proxy_secrets' named lock row could not be taken")

    patch.setattr(core_extras, "acquire_proxy_secrets_lock", busy)


def test_a_wireguard_core_save_that_cannot_take_the_lock_stores_nothing(access_token, monkeypatch):
    core = _wg_core(access_token, unique_name("wg_lock"), ["10.51.1.1/29"])
    try:
        with monkeypatch.context() as patch:
            _lock_is_busy(patch)
            response = _put_core(access_token, core, ["10.51.1.1/28"])

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, response.text
        assert response.json()["detail"] == LOCK_UNAVAILABLE_DETAIL
        assert _stored_core(access_token, core["id"]).json()["config"]["address"] == ["10.51.1.1/29"]
    finally:
        delete_core(access_token, core["id"])


def test_a_wireguard_core_save_whose_reconcile_fails_stores_nothing(access_token, monkeypatch):
    from app.operation import core as core_module

    core = _wg_core(access_token, unique_name("wg_lock"), ["10.51.2.1/29"])
    try:

        async def reconcile_fails(db):
            raise OperationalError("SELECT wireguard_subnets FOR UPDATE", {}, _LockWaitTimeout())

        with monkeypatch.context() as patch:
            patch.setattr(core_module, "reconcile_wireguard_subnets", reconcile_fails)
            response = _put_core(access_token, core, ["10.51.2.1/28"])

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, response.text
        assert _stored_core(access_token, core["id"]).json()["config"]["address"] == ["10.51.2.1/29"]
    finally:
        delete_core(access_token, core["id"])


def test_a_wireguard_core_delete_that_cannot_take_the_lock_keeps_the_core(access_token, monkeypatch):
    core = _wg_core(access_token, unique_name("wg_lock"), ["10.51.3.1/29"])
    try:
        with monkeypatch.context() as patch:
            _lock_is_busy(patch)
            response = client.delete(
                f"/api/core/{core['id']}", headers=auth_headers(access_token), params={"restart_nodes": False}
            )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, response.text
        assert response.json()["detail"] == LOCK_UNAVAILABLE_DETAIL
        assert _stored_core(access_token, core["id"]).status_code == status.HTTP_200_OK
    finally:
        delete_core(access_token, core["id"])


def test_a_wireguard_core_created_while_the_lock_is_busy_is_saved_and_its_first_user_gets_an_address(
    access_token, monkeypatch, caplog
):
    interface_name = unique_name("wg_lock")
    with monkeypatch.context() as patch, caplog.at_level(logging.WARNING):
        _lock_is_busy(patch)
        response = client.post(
            "/api/core",
            headers=auth_headers(access_token),
            json={
                "config": _wg_config(interface_name, ["10.51.4.1/29"]),
                "name": unique_name("wg_lock_core"),
                "type": "wg",
                "exclude_inbound_tags": [],
                "fallbacks_inbound_tags": [],
            },
        )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    core = response.json()
    group = create_group(access_token, name=unique_name("wg_lock_group"), inbound_tags=[interface_name])
    user = None
    try:
        assert any("pool rows were not created" in record.getMessage() for record in caplog.records)
        user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("wg_lock_u")})
        assert user["proxy_settings"]["wireguard"]["peer_ips"] == ["10.51.4.2/32"]
    finally:
        if user is not None:
            delete_user(access_token, user["username"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])
