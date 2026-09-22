from types import SimpleNamespace

from app.db.crud.wireguard import _ensure_wireguard_keys, _user_public_key
from app.utils.crypto import generate_wireguard_keypair, get_wireguard_public_key

SHARED_PRIVATE, SHARED_PUBLIC = generate_wireguard_keypair()


def _user(private_key=None, public_key=None):
    wg = {}
    if private_key is not None:
        wg["private_key"] = private_key
    if public_key is not None:
        wg["public_key"] = public_key
    return SimpleNamespace(proxy_settings={"wireguard": wg})


def test_a_shared_public_key_is_replaced_by_a_fresh_pair():
    user = _user(SHARED_PRIVATE, SHARED_PUBLIC)

    assert _ensure_wireguard_keys(user, frozenset({SHARED_PUBLIC})) is True

    wg = user.proxy_settings["wireguard"]
    assert wg["public_key"] != SHARED_PUBLIC
    assert wg["private_key"] != SHARED_PRIVATE
    assert get_wireguard_public_key(wg["private_key"]) == wg["public_key"]


def test_two_users_on_one_shared_key_end_up_with_different_keys():
    first = _user(SHARED_PRIVATE, SHARED_PUBLIC)
    second = _user(SHARED_PRIVATE, SHARED_PUBLIC)
    shared = frozenset({SHARED_PUBLIC})

    _ensure_wireguard_keys(first, shared)
    _ensure_wireguard_keys(second, shared)

    assert _user_public_key(first.proxy_settings) != _user_public_key(second.proxy_settings)
    assert _user_public_key(first.proxy_settings) != SHARED_PUBLIC
    assert _user_public_key(second.proxy_settings) != SHARED_PUBLIC


def test_an_unshared_complete_keypair_is_left_alone():
    private_key, public_key = generate_wireguard_keypair()
    user = _user(private_key, public_key)

    assert _ensure_wireguard_keys(user, frozenset({SHARED_PUBLIC})) is False
    assert user.proxy_settings["wireguard"]["private_key"] == private_key
    assert user.proxy_settings["wireguard"]["public_key"] == public_key


def test_a_missing_public_key_is_derived_not_regenerated():
    private_key, public_key = generate_wireguard_keypair()
    user = _user(private_key)

    assert _ensure_wireguard_keys(user) is True
    assert user.proxy_settings["wireguard"]["private_key"] == private_key
    assert user.proxy_settings["wireguard"]["public_key"] == public_key


def test_a_shared_private_key_cannot_sneak_its_public_key_back_in_by_derivation():
    user = _user(SHARED_PRIVATE)

    assert _ensure_wireguard_keys(user, frozenset({SHARED_PUBLIC})) is True

    wg = user.proxy_settings["wireguard"]
    assert wg["public_key"] != SHARED_PUBLIC
    assert wg["private_key"] != SHARED_PRIVATE
    assert get_wireguard_public_key(wg["private_key"]) == wg["public_key"]


def test_a_user_with_no_keys_gets_a_fresh_pair():
    user = _user()

    assert _ensure_wireguard_keys(user) is True

    wg = user.proxy_settings["wireguard"]
    assert wg["private_key"]
    assert get_wireguard_public_key(wg["private_key"]) == wg["public_key"]


def test_two_private_only_users_sharing_a_key_are_both_detected():
    from app.db.crud.wireguard import shared_wireguard_public_keys

    class _Result:
        def __iter__(self):
            return iter(())

    class _Db:
        async def execute(self, _stmt):
            return _Result()

    users = [_user(SHARED_PRIVATE), _user(SHARED_PRIVATE)]

    import asyncio

    shared = asyncio.run(shared_wireguard_public_keys(_Db(), users))

    assert SHARED_PUBLIC in shared


def test_a_lone_private_only_user_is_not_flagged():
    from app.db.crud.wireguard import shared_wireguard_public_keys

    class _Result:
        def __iter__(self):
            return iter(())

    class _Db:
        async def execute(self, _stmt):
            return _Result()

    private_key, public_key = generate_wireguard_keypair()

    import asyncio

    shared = asyncio.run(shared_wireguard_public_keys(_Db(), [_user(private_key)]))

    assert public_key not in shared


def test_the_empty_shared_set_preserves_the_old_behaviour():
    user = _user(SHARED_PRIVATE, SHARED_PUBLIC)

    assert _ensure_wireguard_keys(user, frozenset()) is False
    assert user.proxy_settings["wireguard"]["public_key"] == SHARED_PUBLIC
