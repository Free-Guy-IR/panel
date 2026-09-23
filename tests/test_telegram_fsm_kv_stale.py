import pytest
from aiogram.fsm.storage.base import StorageKey

from app.nats.kv_cas import MemoryCasKv
from app.telegram.fsm_storage import NatsFSMStorage


class _FlakyKV(MemoryCasKv):
    def __init__(self):
        super().__init__()
        self.fail_writes = 0

    def _maybe_fail(self):
        if self.fail_writes:
            self.fail_writes -= 1
            raise ConnectionError("nats: timeout")

    async def put(self, key, value):
        self._maybe_fail()
        self._rev += 1
        self._data[key] = (value, self._rev)
        return self._rev

    async def delete(self, key, last=None):
        self._maybe_fail()
        self._data.pop(key, None)
        return True


def _storage(kv) -> NatsFSMStorage:
    storage = NatsFSMStorage("telegram-test")
    storage._nats_enabled = True
    storage._kv = kv
    return storage


KEY = StorageKey(bot_id=1, chat_id=2, user_id=3)


@pytest.mark.asyncio
async def test_a_failed_state_write_never_brings_the_older_state_back():
    kv = _FlakyKV()
    storage = _storage(kv)
    await storage.set_state(KEY, "choose_plan")

    kv.fail_writes = 1
    await storage.set_state(KEY, "confirm_payment")

    assert await storage.get_state(KEY) == "confirm_payment"


@pytest.mark.asyncio
async def test_a_failed_state_delete_never_brings_the_finished_conversation_back():
    kv = _FlakyKV()
    storage = _storage(kv)
    await storage.set_state(KEY, "confirm_payment")

    kv.fail_writes = 1
    await storage.set_state(KEY, None)

    assert await storage.get_state(KEY) is None


@pytest.mark.asyncio
async def test_a_failed_data_write_never_brings_the_older_data_back():
    kv = _FlakyKV()
    storage = _storage(kv)
    await storage.set_data(KEY, {"plan": "basic"})

    kv.fail_writes = 1
    await storage.set_data(KEY, {"plan": "premium"})
    assert await storage.get_data(KEY) == {"plan": "premium"}

    kv.fail_writes = 1
    await storage.set_data(KEY, {})
    assert await storage.get_data(KEY) == {}


@pytest.mark.asyncio
async def test_a_write_that_failed_reaches_the_kv_once_it_is_back_so_other_workers_see_it():
    kv = _FlakyKV()
    storage = _storage(kv)
    await storage.set_state(KEY, "choose_plan")
    kv.fail_writes = 1
    await storage.set_state(KEY, "confirm_payment")

    assert await storage.get_state(KEY) == "confirm_payment"

    other_worker = _storage(kv)
    assert await other_worker.get_state(KEY) == "confirm_payment"


@pytest.mark.asyncio
async def test_a_write_made_while_the_kv_was_unreachable_wins_over_the_kv_value_afterwards():
    kv = _FlakyKV()
    storage = _storage(kv)
    await storage.set_state(KEY, "choose_plan")

    storage._kv = None
    storage._next_connect_try_at = float("inf")
    await storage.set_state(KEY, "confirm_payment")
    storage._kv = kv
    storage._next_connect_try_at = 0.0

    assert await storage.get_state(KEY) == "confirm_payment"
