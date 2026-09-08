import pytest

from app.models.proxy import ProxyTable
from app.models.user import BulkUserFilter
from app.operation import OperatorType
from app.operation.user import UserOperation


class _FakeUser:
    def __init__(self, user_id, proxy_settings, groups):
        self.id = user_id
        self.proxy_settings = proxy_settings
        self.groups = groups


class _FakeDB:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.fixture
def patched(monkeypatch):
    from app.operation import user as user_module

    state = {"candidates": [], "synced": None}

    async def fake_candidates(db, bulk_model):
        return state["candidates"]

    async def fake_prepare(db, proxy_settings, groups):
        if "l2tp" in groups and not proxy_settings.l2tp.password:
            proxy_settings.l2tp.password = "GeneratedPassword123"
        return proxy_settings

    async def fake_sync(users):
        state["synced"] = users

    monkeypatch.setattr(user_module, "get_users_for_l2tp_activation", fake_candidates)
    monkeypatch.setattr(user_module, "prepare_l2tp_password", fake_prepare)
    monkeypatch.setattr(user_module, "sync_users", fake_sync)
    return state


def _operation():
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API
    return operation


@pytest.mark.asyncio
async def test_dry_run_counts_without_writing(patched):
    patched["candidates"] = [
        _FakeUser(1, {}, ["l2tp"]),
        _FakeUser(2, {"l2tp": {"password": "AlreadyHasOne12345678"}}, ["l2tp"]),
        _FakeUser(3, {}, ["vless-only"]),
    ]
    db = _FakeDB()

    result = await _operation().bulk_activate_l2tp_passwords(db, BulkUserFilter(dry_run=True))

    assert result.affected_users == 1
    assert db.commits == 0
    assert patched["synced"] is None
    assert patched["candidates"][0].proxy_settings == {}


@pytest.mark.asyncio
async def test_only_eligible_users_without_a_password_are_updated(patched):
    eligible = _FakeUser(1, {}, ["l2tp"])
    already = _FakeUser(2, {"l2tp": {"password": "AlreadyHasOne12345678"}}, ["l2tp"])
    no_access = _FakeUser(3, {}, ["vless-only"])
    patched["candidates"] = [eligible, already, no_access]
    db = _FakeDB()

    result = await _operation().bulk_activate_l2tp_passwords(db, BulkUserFilter())

    assert result == {"detail": "operation has been successfuly done on 1 users"}
    assert db.commits == 1
    assert eligible.proxy_settings["l2tp"]["password"] == "GeneratedPassword123"
    assert already.proxy_settings["l2tp"]["password"] == "AlreadyHasOne12345678"
    assert no_access.proxy_settings == {}


@pytest.mark.asyncio
async def test_only_changed_users_are_pushed_to_the_nodes(patched):
    eligible = _FakeUser(1, {}, ["l2tp"])
    patched["candidates"] = [eligible, _FakeUser(2, {"l2tp": {"password": "AlreadyHasOne12345678"}}, ["l2tp"])]

    await _operation().bulk_activate_l2tp_passwords(_FakeDB(), BulkUserFilter())

    assert patched["synced"] == [eligible]


@pytest.mark.asyncio
async def test_nothing_to_do_commits_nothing_and_syncs_nothing(patched):
    patched["candidates"] = [_FakeUser(1, {"l2tp": {"password": "AlreadyHasOne12345678"}}, ["l2tp"])]
    db = _FakeDB()

    result = await _operation().bulk_activate_l2tp_passwords(db, BulkUserFilter())

    assert result == {"detail": "operation has been successfuly done on 0 users"}
    assert db.commits == 0
    assert patched["synced"] is None


@pytest.mark.asyncio
async def test_issued_passwords_survive_proxy_table_validation(patched):
    eligible = _FakeUser(1, {}, ["l2tp"])
    patched["candidates"] = [eligible]

    await _operation().bulk_activate_l2tp_passwords(_FakeDB(), BulkUserFilter())

    assert ProxyTable.model_validate(eligible.proxy_settings).l2tp.password == "GeneratedPassword123"
