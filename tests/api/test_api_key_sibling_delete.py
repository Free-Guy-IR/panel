from fastapi import status

from tests.api import client
from tests.api.concurrent_requests import send_concurrently
from tests.api.test_api_key_sibling_update import _create_key, _create_owner, _drop, _session

KEY_DELETER = {"api_keys": {"read": True, "delete": True}}
BROAD = {"api_keys": {"read": True, "update": True, "delete": True}, "admins": {"read": True, "update": True}}


def _exists(session: dict[str, str], key_id: int) -> bool:
    response = client.get(f"/api/api_key/{key_id}", headers=session)
    assert response.status_code in (status.HTTP_200_OK, status.HTTP_404_NOT_FOUND), response.text
    return response.status_code == status.HTTP_200_OK


def _bulk_delete(headers: dict[str, str], ids: list[int]):
    return client.post("/api/api_keys/bulk/delete", headers=headers, json={"ids": ids})


def test_a_narrow_key_cannot_delete_a_broader_sibling_key():
    owner = _create_owner()
    try:
        session = _session(owner)
        narrow = _create_key(session, KEY_DELETER)
        broad = _create_key(session, BROAD)

        response = client.delete(f"/api/api_key/{broad['id']}", headers={"X-Api-Key": narrow["api_key"]})

        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        assert _exists(session, broad["id"])
    finally:
        _drop(owner["username"])


def test_a_custom_key_cannot_delete_an_inheriting_sibling_key():
    owner = _create_owner()
    try:
        session = _session(owner)
        narrow = _create_key(session, KEY_DELETER)
        bot_key = _create_key(session, None)

        response = client.delete(f"/api/api_key/{bot_key['id']}", headers={"X-Api-Key": narrow["api_key"]})

        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        assert _exists(session, bot_key["id"])
    finally:
        _drop(owner["username"])


def test_a_narrow_key_cannot_bulk_delete_a_broader_sibling_key():
    owner = _create_owner()
    try:
        session = _session(owner)
        narrow = _create_key(session, KEY_DELETER)
        within = _create_key(session, {"api_keys": {"read": True}})
        broad = _create_key(session, BROAD)

        response = _bulk_delete({"X-Api-Key": narrow["api_key"]}, [within["id"], broad["id"]])

        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        assert _exists(session, broad["id"])
        assert _exists(session, within["id"])
    finally:
        _drop(owner["username"])


def test_a_custom_key_cannot_bulk_delete_an_inheriting_sibling_key():
    owner = _create_owner()
    try:
        session = _session(owner)
        narrow = _create_key(session, KEY_DELETER)
        bot_key = _create_key(session, None)

        response = _bulk_delete({"X-Api-Key": narrow["api_key"]}, [bot_key["id"]])

        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        assert _exists(session, bot_key["id"])
    finally:
        _drop(owner["username"])


def test_a_key_may_still_delete_a_sibling_within_its_own_permissions_and_itself():
    owner = _create_owner()
    try:
        session = _session(owner)
        deleter = _create_key(session, KEY_DELETER)
        within = _create_key(session, {"api_keys": {"read": True}})
        bulk_within = _create_key(session, {"api_keys": {"delete": True}})

        single = client.delete(f"/api/api_key/{within['id']}", headers={"X-Api-Key": deleter["api_key"]})
        bulk = _bulk_delete({"X-Api-Key": deleter["api_key"]}, [bulk_within["id"]])
        own = client.delete(f"/api/api_key/{deleter['id']}", headers={"X-Api-Key": deleter["api_key"]})

        assert single.status_code == status.HTTP_204_NO_CONTENT, single.text
        assert bulk.status_code == status.HTTP_200_OK, bulk.text
        assert bulk.json()["count"] == 1
        assert own.status_code == status.HTTP_204_NO_CONTENT, own.text
        assert not _exists(session, within["id"])
        assert not _exists(session, bulk_within["id"])
        assert not _exists(session, deleter["id"])
    finally:
        _drop(owner["username"])


def test_a_custom_key_may_bulk_delete_itself():
    owner = _create_owner()
    try:
        session = _session(owner)
        deleter = _create_key(session, KEY_DELETER)

        response = _bulk_delete({"X-Api-Key": deleter["api_key"]}, [deleter["id"]])

        assert response.status_code == status.HTTP_200_OK, response.text
        assert not _exists(session, deleter["id"])
    finally:
        _drop(owner["username"])


def test_inheriting_owner_keys_like_the_bots_still_delete_any_sibling():
    owner = _create_owner()
    try:
        session = _session(owner)
        bot_keys = [_create_key(session, None) for _ in range(7)]
        broad = _create_key(session, BROAD)
        other_bot = _create_key(session, None)
        bulk_targets = [_create_key(session, BROAD), _create_key(session, None)]

        single = client.delete(f"/api/api_key/{broad['id']}", headers={"X-Api-Key": bot_keys[0]["api_key"]})
        inheriting = client.delete(f"/api/api_key/{other_bot['id']}", headers={"X-Api-Key": bot_keys[1]["api_key"]})
        bulk = _bulk_delete({"X-Api-Key": bot_keys[2]["api_key"]}, [key["id"] for key in bulk_targets])

        assert single.status_code == status.HTTP_204_NO_CONTENT, single.text
        assert inheriting.status_code == status.HTTP_204_NO_CONTENT, inheriting.text
        assert bulk.status_code == status.HTTP_200_OK, bulk.text
        assert bulk.json()["count"] == 2
        for bot_key in bot_keys:
            assert client.get("/api/admin", headers={"X-Api-Key": bot_key["api_key"]}).status_code == status.HTTP_200_OK
    finally:
        _drop(owner["username"])


def test_concurrent_sibling_deletes_by_a_narrow_key_all_fail_while_the_session_succeeds():
    owner = _create_owner()
    try:
        session = _session(owner)
        narrow = _create_key(session, KEY_DELETER)
        broad = _create_key(session, BROAD)
        bulk_broad = _create_key(session, BROAD)

        url = f"/api/api_key/{broad['id']}"
        narrow_headers = {"X-Api-Key": narrow["api_key"]}
        bulk_call = (
            "POST",
            "/api/api_keys/bulk/delete",
            {"headers": narrow_headers, "json": {"ids": [bulk_broad["id"]]}},
        )
        calls = [("DELETE", url, {"headers": narrow_headers})] * 3 + [bulk_call] * 2
        narrow_codes = [response.status_code for response in send_concurrently(calls)]

        assert narrow_codes == [status.HTTP_403_FORBIDDEN] * 5
        assert _exists(session, broad["id"])
        assert _exists(session, bulk_broad["id"])

        calls = [("DELETE", url, {"headers": session})] + [("DELETE", url, {"headers": narrow_headers})] * 2
        session_code, *late_codes = [response.status_code for response in send_concurrently(calls)]

        assert session_code == status.HTTP_204_NO_CONTENT
        assert all(code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND) for code in late_codes)
        assert not _exists(session, broad["id"])
    finally:
        _drop(owner["username"])
