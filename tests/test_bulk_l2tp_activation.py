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

    async def fake_cores(db):
        return ["core"]

    def fake_tags(cores):
        return {"l2tp-de"}

    async def fake_tags_from_groups(groups):
        return set(groups)

    async def fake_sync(users):
        state["synced"] = users

    monkeypatch.setattr(user_module, "get_users_for_l2tp_activation", fake_candidates)
    monkeypatch.setattr(user_module, "get_l2tp_cores", fake_cores)
    monkeypatch.setattr(user_module, "l2tp_core_tags", fake_tags)
    monkeypatch.setattr(user_module, "tags_from_groups", fake_tags_from_groups)
    monkeypatch.setattr(user_module, "sync_users", fake_sync)
    return state


def _operation():
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API
    return operation


def _eligible(user_id=1, settings=None):
    return _FakeUser(user_id, {} if settings is None else settings, ["l2tp-de"])


def _already(user_id=2):
    return _FakeUser(user_id, {"l2tp": {"password": "AlreadyHasOne12345678"}}, ["l2tp-de"])


def _no_access(user_id=3):
    return _FakeUser(user_id, {}, ["vless-only"])


@pytest.mark.asyncio
async def test_dry_run_counts_without_writing(patched):
    patched["candidates"] = [_eligible(), _already(), _no_access()]
    db = _FakeDB()

    result = await _operation().bulk_activate_l2tp_passwords(db, BulkUserFilter(dry_run=True))

    assert result.affected_users == 1
    assert db.commits == 0
    assert patched["synced"] is None
    assert patched["candidates"][0].proxy_settings == {}


@pytest.mark.asyncio
async def test_only_eligible_users_without_a_password_are_updated(patched):
    eligible, already, no_access = _eligible(), _already(), _no_access()
    patched["candidates"] = [eligible, already, no_access]
    db = _FakeDB()

    result = await _operation().bulk_activate_l2tp_passwords(db, BulkUserFilter())

    assert result == {"detail": "operation has been successfuly done on 1 users"}
    assert db.commits == 1
    assert ProxyTable.model_validate(eligible.proxy_settings).l2tp.password
    assert already.proxy_settings["l2tp"]["password"] == "AlreadyHasOne12345678"
    assert no_access.proxy_settings == {}


@pytest.mark.asyncio
async def test_only_the_l2tp_key_is_written_and_other_protocols_are_left_alone(patched):
    stored = {"vmess": {"id": "b7b0f6ee-3e1b-4a9c-9d8e-4a0e1f2c3d4e"}, "custom_future_key": {"kept": True}}
    eligible = _eligible(settings=dict(stored))
    patched["candidates"] = [eligible]

    await _operation().bulk_activate_l2tp_passwords(_FakeDB(), BulkUserFilter())

    assert set(eligible.proxy_settings) == {"vmess", "custom_future_key", "l2tp"}
    assert eligible.proxy_settings["vmess"] == stored["vmess"]
    assert eligible.proxy_settings["custom_future_key"] == {"kept": True}


@pytest.mark.asyncio
async def test_issued_passwords_are_20_alphanumeric_characters(patched):
    eligible = _eligible()
    patched["candidates"] = [eligible]

    await _operation().bulk_activate_l2tp_passwords(_FakeDB(), BulkUserFilter())

    password = eligible.proxy_settings["l2tp"]["password"]
    assert len(password) == 20
    assert password.isalnum()


@pytest.mark.asyncio
async def test_every_user_gets_a_distinct_password(patched):
    users = [_eligible(user_id=i) for i in range(30)]
    patched["candidates"] = users

    await _operation().bulk_activate_l2tp_passwords(_FakeDB(), BulkUserFilter())

    issued = {u.proxy_settings["l2tp"]["password"] for u in users}
    assert len(issued) == 30


@pytest.mark.asyncio
async def test_one_unreadable_user_does_not_abort_the_run(patched):
    broken = _FakeUser(9, {"wireguard": {"private_key": "not-a-real-key"}}, ["l2tp-de"])
    eligible = _eligible()
    patched["candidates"] = [broken, eligible]
    db = _FakeDB()

    result = await _operation().bulk_activate_l2tp_passwords(db, BulkUserFilter())

    assert result == {"detail": "operation has been successfuly done on 1 users"}
    assert db.commits == 1
    assert eligible.proxy_settings["l2tp"]["password"]
    assert patched["synced"] == [eligible]


@pytest.mark.asyncio
async def test_only_changed_users_are_pushed_to_the_nodes(patched):
    eligible = _eligible()
    patched["candidates"] = [eligible, _already()]

    await _operation().bulk_activate_l2tp_passwords(_FakeDB(), BulkUserFilter())

    assert patched["synced"] == [eligible]


@pytest.mark.asyncio
async def test_nothing_to_do_commits_nothing_and_syncs_nothing(patched):
    patched["candidates"] = [_already()]
    db = _FakeDB()

    result = await _operation().bulk_activate_l2tp_passwords(db, BulkUserFilter())

    assert result == {"detail": "operation has been successfuly done on 0 users"}
    assert db.commits == 0
    assert patched["synced"] is None


@pytest.mark.asyncio
async def test_no_l2tp_core_means_nobody_is_touched(patched, monkeypatch):
    from app.operation import user as user_module

    monkeypatch.setattr(user_module, "l2tp_core_tags", lambda cores: set())
    patched["candidates"] = [_eligible()]
    db = _FakeDB()

    result = await _operation().bulk_activate_l2tp_passwords(db, BulkUserFilter(dry_run=True))

    assert result.affected_users == 0
    assert db.commits == 0
