import asyncio

import pytest
from fastapi import HTTPException, status

import app.core.hosts as hosts_module
from app.core.hosts import host_manager
from app.models.admin import AdminDetails, AdminRoleData
from app.models.host import BulkHostSelection, CreateHost
from app.operation import OperatorType
from app.operation.host import HostOperation
from tests.api import GetTestDB, client
from tests.api.helpers import auth_headers, create_core, delete_core, get_inbounds, unique_name

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
MISSING_DOWNLOAD_HOST_ID = 987654321
MANAGER_PAUSE_TARGETS = {"add_hosts": "add_hosts", "apply": "_prepare_hosts"}
PAUSE_AFTER_THE_REAL_CALL = {"apply"}


class _EchoRouter:
    async def publish(self, topic, message):
        await host_manager._handle_host_message(message)


@pytest.fixture(params=["configured", "nats"])
def host_write_path(request, monkeypatch):
    if request.param == "nats":
        monkeypatch.setattr(hosts_module, "router", _EchoRouter())
        monkeypatch.setattr(host_manager, "_add_hosts_impl", host_manager._add_hosts_nats)
        monkeypatch.setattr(host_manager, "_remove_host_impl", host_manager._remove_host_nats)
    return request.param


def _create_host(access_token: str, inbound: str, address: str, is_disabled: bool) -> int:
    response = client.post(
        "/api/host",
        headers=auth_headers(access_token),
        json={
            "remark": unique_name("it2_host"),
            "address": [address],
            "port": 443,
            "inbound_tag": inbound,
            "priority": 1,
            "is_disabled": is_disabled,
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()["id"]


def _db_host(access_token: str, host_id: int) -> dict | None:
    response = client.get(f"/api/host/{host_id}", headers=auth_headers(access_token))
    if response.status_code == status.HTTP_404_NOT_FOUND:
        return None
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def _with_address(payload: dict, address: str) -> dict:
    return {**payload, "address": [address]}


def _stored_remarks(access_token: str) -> set[str]:
    response = client.get("/api/hosts", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    items = body if isinstance(body, list) else body.get("hosts", [])
    return {item["remark"] for item in items}


def _assert_cache_matches_db(access_token: str, host_ids: list[int]) -> None:
    for host_id in host_ids:
        stored = _db_host(access_token, host_id)
        cached = host_manager._hosts.get(host_id)
        if stored is None or stored["is_disabled"]:
            assert cached is None, (host_id, cached)
            continue
        assert cached is not None, host_id
        cached_address = cached.address if isinstance(cached.address, list) else [cached.address]
        assert set(cached_address) == set(stored["address"]), (host_id, cached_address, stored["address"])
        assert cached.remark == stored["remark"], (host_id, cached.remark, stored["remark"])


def _gate_host_manager(monkeypatch, pause_at: str) -> tuple[asyncio.Event, asyncio.Event]:
    paused, resume = asyncio.Event(), asyncio.Event()
    armed = [True]
    target = MANAGER_PAUSE_TARGETS[pause_at]
    real = getattr(host_manager, target)

    async def gated(*args, **kwargs):
        if pause_at in PAUSE_AFTER_THE_REAL_CALL:
            result = await real(*args, **kwargs)
            if armed[0]:
                armed[0] = False
                paused.set()
                await resume.wait()
            return result
        if armed[0]:
            armed[0] = False
            paused.set()
            await resume.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(host_manager, target, gated)
    return paused, resume


def _names_missing_download_host(host) -> bool:
    xhttp = getattr(getattr(host, "transport_settings", None), "xhttp_settings", None)
    return getattr(xhttp, "download_settings", None) == MISSING_DOWNLOAD_HOST_ID


def _gate_batch_validation_of_the_broken_host(monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
    paused, resume = asyncio.Event(), asyncio.Event()
    armed = [True]
    real = HostOperation.validate_ds_host

    async def gated(self, db, host, *args, **kwargs):
        if armed[0] and _names_missing_download_host(host):
            armed[0] = False
            paused.set()
            await resume.wait()
        return await real(self, db, host, *args, **kwargs)

    monkeypatch.setattr(HostOperation, "validate_ds_host", gated)
    return paused, resume


async def _save_batch(payloads: list[dict]) -> None:
    async with GetTestDB() as db:
        await HostOperation(operator_type=OperatorType.API).modify_hosts(
            db, [CreateHost.model_validate(payload) for payload in payloads], OWNER
        )


async def _toggle(host_ids: list[int], is_disabled: bool) -> None:
    async with GetTestDB() as db:
        await HostOperation(operator_type=OperatorType.API).bulk_set_hosts_disabled(
            db, BulkHostSelection(ids=host_ids), OWNER, is_disabled=is_disabled
        )


async def _save_batch_expecting_not_found(payloads: list[dict]) -> None:
    with pytest.raises(HTTPException) as caught:
        await _save_batch(payloads)
    assert caught.value.status_code == status.HTTP_404_NOT_FOUND, caught.value.detail


def _cleanup(access_token: str, host_ids: list[int], core_id: int) -> None:
    client.post("/api/hosts/bulk/delete", headers=auth_headers(access_token), json={"ids": host_ids})
    delete_core(access_token, core_id)


@pytest.mark.parametrize("toggle_to_disabled", [True, False])
@pytest.mark.parametrize("pause_at", ["add_hosts", "apply"])
def test_batch_save_paused_in_the_host_manager_while_a_bulk_toggle_finishes_leaves_the_cache_equal_to_the_db(
    access_token, monkeypatch, host_write_path, pause_at, toggle_to_disabled
):
    core = create_core(access_token, name=unique_name("it2_core"))
    inbound = get_inbounds(access_token)[0]
    shared = _create_host(access_token, inbound, "shared-old.example.com", is_disabled=not toggle_to_disabled)
    other = _create_host(access_token, inbound, "other-old.example.com", is_disabled=False)
    host_ids = [shared, other]
    batch = [
        _with_address(_db_host(access_token, shared), "shared-batch.example.com"),
        _with_address(_db_host(access_token, other), "other-batch.example.com"),
    ]

    async def scenario():
        paused, resume = _gate_host_manager(monkeypatch, pause_at)
        save = asyncio.create_task(_save_batch(batch))
        await asyncio.wait_for(paused.wait(), 10)
        await asyncio.wait_for(_toggle([shared], toggle_to_disabled), 10)
        resume.set()
        await asyncio.wait_for(save, 10)

    try:
        asyncio.run(scenario())
        monkeypatch.undo()
        stored_shared = _db_host(access_token, shared)
        assert stored_shared["is_disabled"] is toggle_to_disabled
        assert stored_shared["address"] == ["shared-batch.example.com"]
        assert _db_host(access_token, other)["address"] == ["other-batch.example.com"]
        assert (shared in host_manager._hosts) is not toggle_to_disabled
        _assert_cache_matches_db(access_token, host_ids)
    finally:
        monkeypatch.undo()
        _cleanup(access_token, host_ids, core["id"])


@pytest.mark.parametrize("pause_at", ["add_hosts", "apply"])
def test_bulk_toggle_paused_in_the_host_manager_while_a_batch_save_commits_leaves_the_cache_equal_to_the_db(
    access_token, monkeypatch, host_write_path, pause_at
):
    core = create_core(access_token, name=unique_name("it2_core"))
    inbound = get_inbounds(access_token)[0]
    shared = _create_host(access_token, inbound, "shared-old.example.com", is_disabled=True)
    other = _create_host(access_token, inbound, "other-old.example.com", is_disabled=False)
    host_ids = [shared, other]

    async def scenario():
        paused, resume = _gate_host_manager(monkeypatch, pause_at)
        toggle = asyncio.create_task(_toggle([shared], False))
        await asyncio.wait_for(paused.wait(), 10)
        batch = [
            _with_address(_db_host(access_token, shared), "shared-batch.example.com"),
            _with_address(_db_host(access_token, other), "other-batch.example.com"),
        ]
        await asyncio.wait_for(_save_batch(batch), 10)
        resume.set()
        await asyncio.wait_for(toggle, 10)

    try:
        asyncio.run(scenario())
        monkeypatch.undo()
        stored_shared = _db_host(access_token, shared)
        assert stored_shared["is_disabled"] is False
        assert stored_shared["address"] == ["shared-batch.example.com"]
        assert _db_host(access_token, other)["address"] == ["other-batch.example.com"]
        assert shared in host_manager._hosts
        _assert_cache_matches_db(access_token, host_ids)
    finally:
        monkeypatch.undo()
        _cleanup(access_token, host_ids, core["id"])


@pytest.mark.parametrize("paused_side", ["batch_save_in_validation", "toggle_in_add_hosts", "toggle_in_apply"])
def test_a_batch_save_failing_half_way_beside_a_bulk_toggle_keeps_nothing_of_the_batch_and_all_of_the_toggle(
    access_token, monkeypatch, host_write_path, paused_side
):
    core = create_core(access_token, name=unique_name("it2_core"))
    inbound = get_inbounds(access_token)[0]
    shared = _create_host(access_token, inbound, "shared-old.example.com", is_disabled=True)
    host_ids = [shared]
    original = _db_host(access_token, shared)
    broken_remark = unique_name("it2_broken")
    batch = [
        {**_with_address(original, "shared-batch.example.com"), "remark": unique_name("it2_renamed")},
        {
            "remark": broken_remark,
            "address": ["broken.example.com"],
            "port": 443,
            "inbound_tag": inbound,
            "priority": 2,
            "transport_settings": {"xhttp_settings": {"download_settings": MISSING_DOWNLOAD_HOST_ID}},
        },
    ]

    async def scenario():
        if paused_side == "batch_save_in_validation":
            paused, resume = _gate_batch_validation_of_the_broken_host(monkeypatch)
            save = asyncio.create_task(_save_batch_expecting_not_found(batch))
            await asyncio.wait_for(paused.wait(), 10)
            await asyncio.wait_for(_toggle([shared], False), 10)
            resume.set()
            await asyncio.wait_for(save, 10)
        else:
            paused, resume = _gate_host_manager(monkeypatch, paused_side.removeprefix("toggle_in_"))
            toggle = asyncio.create_task(_toggle([shared], False))
            await asyncio.wait_for(paused.wait(), 10)
            await asyncio.wait_for(_save_batch_expecting_not_found(batch), 10)
            resume.set()
            await asyncio.wait_for(toggle, 10)

    try:
        asyncio.run(scenario())
        monkeypatch.undo()
        stored = _db_host(access_token, shared)
        assert stored["is_disabled"] is False
        assert stored["address"] == original["address"]
        assert stored["remark"] == original["remark"]
        assert broken_remark not in _stored_remarks(access_token)
        assert broken_remark not in {cached.remark for cached in host_manager._hosts.values()}
        assert shared in host_manager._hosts
        _assert_cache_matches_db(access_token, host_ids)
    finally:
        monkeypatch.undo()
        _cleanup(access_token, host_ids, core["id"])
