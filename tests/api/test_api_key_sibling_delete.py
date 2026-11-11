from fastapi import status

from app.models.api_key import BulkAPIKeySelection
from tests.api import client
from tests.api.api_key_actor import OPERATION_DONE, run_as_key
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


def _selection(ids: list[int]) -> BulkAPIKeySelection:
    return BulkAPIKeySelection(ids=ids)


def test_a_narrow_key_cannot_delete_a_broader_sibling_key():
    owner = _create_owner()
    try:
        session = _session(owner)
        narrow = _create_key(session, KEY_DELETER)
        broad = _create_key(session, BROAD)

        response = client.delete(f"/api/api_key/{broad['id']}", headers={"X-Api-Key": narrow["api_key"]})
        operation = run_as_key(narrow["api_key"], "delete_api_key", key_id=broad["id"])

        assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.text
        assert operation == status.HTTP_403_FORBIDDEN
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
        operation = run_as_key(narrow["api_key"], "delete_api_key", key_id=bot_key["id"])

        assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.text
        assert operation == status.HTTP_403_FORBIDDEN
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
        ids = [within["id"], broad["id"]]

        response = _bulk_delete({"X-Api-Key": narrow["api_key"]}, ids)
        operation = run_as_key(narrow["api_key"], "bulk_delete_api_keys", bulk_api_keys=_selection(ids))

        assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.text
        assert operation == status.HTTP_403_FORBIDDEN
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
        operation = run_as_key(narrow["api_key"], "bulk_delete_api_keys", bulk_api_keys=_selection([bot_key["id"]]))

        assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.text
        assert operation == status.HTTP_403_FORBIDDEN
        assert _exists(session, bot_key["id"])
    finally:
        _drop(owner["username"])


def test_a_key_deletes_a_sibling_within_its_permissions_and_itself_only_through_the_operation():
    owner = _create_owner()
    try:
        session = _session(owner)
        deleter = _create_key(session, KEY_DELETER)
        within = _create_key(session, {"api_keys": {"read": True}})
        bulk_within = _create_key(session, {"api_keys": {"delete": True}})
        key = {"X-Api-Key": deleter["api_key"]}

        refused = [
            client.delete(f"/api/api_key/{within['id']}", headers=key).status_code,
            _bulk_delete(key, [bulk_within["id"]]).status_code,
            client.delete(f"/api/api_key/{deleter['id']}", headers=key).status_code,
        ]
        assert refused == [status.HTTP_401_UNAUTHORIZED] * 3
        assert all(_exists(session, target["id"]) for target in (within, bulk_within, deleter))

        single = run_as_key(deleter["api_key"], "delete_api_key", key_id=within["id"])
        bulk = run_as_key(deleter["api_key"], "bulk_delete_api_keys", bulk_api_keys=_selection([bulk_within["id"]]))
        own = run_as_key(deleter["api_key"], "delete_api_key", key_id=deleter["id"])

        assert [single, bulk, own] == [OPERATION_DONE] * 3
        assert not _exists(session, within["id"])
        assert not _exists(session, bulk_within["id"])
        assert not _exists(session, deleter["id"])
    finally:
        _drop(owner["username"])


def test_a_custom_key_bulk_deletes_itself_only_through_the_operation():
    owner = _create_owner()
    try:
        session = _session(owner)
        deleter = _create_key(session, KEY_DELETER)

        response = _bulk_delete({"X-Api-Key": deleter["api_key"]}, [deleter["id"]])
        assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.text
        assert _exists(session, deleter["id"])

        operation = run_as_key(deleter["api_key"], "bulk_delete_api_keys", bulk_api_keys=_selection([deleter["id"]]))
        assert operation == OPERATION_DONE
        assert not _exists(session, deleter["id"])
    finally:
        _drop(owner["username"])


def test_inheriting_owner_keys_like_the_bots_delete_siblings_only_through_the_operation():
    owner = _create_owner()
    try:
        session = _session(owner)
        bot_keys = [_create_key(session, None) for _ in range(7)]
        broad = _create_key(session, BROAD)
        other_bot = _create_key(session, None)
        bulk_targets = [_create_key(session, BROAD), _create_key(session, None)]
        bulk_ids = [key["id"] for key in bulk_targets]

        refused = [
            client.delete(f"/api/api_key/{broad['id']}", headers={"X-Api-Key": bot_keys[0]["api_key"]}).status_code,
            client.delete(f"/api/api_key/{other_bot['id']}", headers={"X-Api-Key": bot_keys[1]["api_key"]}).status_code,
            _bulk_delete({"X-Api-Key": bot_keys[2]["api_key"]}, bulk_ids).status_code,
        ]
        assert refused == [status.HTTP_401_UNAUTHORIZED] * 3
        assert all(_exists(session, target["id"]) for target in (broad, other_bot, *bulk_targets))

        single = run_as_key(bot_keys[0]["api_key"], "delete_api_key", key_id=broad["id"])
        inheriting = run_as_key(bot_keys[1]["api_key"], "delete_api_key", key_id=other_bot["id"])
        bulk = run_as_key(bot_keys[2]["api_key"], "bulk_delete_api_keys", bulk_api_keys=_selection(bulk_ids))

        assert [single, inheriting, bulk] == [OPERATION_DONE] * 3
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

        assert narrow_codes == [status.HTTP_401_UNAUTHORIZED] * 5
        assert _exists(session, broad["id"])
        assert _exists(session, bulk_broad["id"])

        calls = [("DELETE", url, {"headers": session})] + [("DELETE", url, {"headers": narrow_headers})] * 2
        session_code, *late_codes = [response.status_code for response in send_concurrently(calls)]

        assert session_code == status.HTTP_204_NO_CONTENT
        assert late_codes == [status.HTTP_401_UNAUTHORIZED] * 2
        assert not _exists(session, broad["id"])
    finally:
        _drop(owner["username"])
