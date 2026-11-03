import random

import pytest

from app.nats import node_rpc
from app.nats.node_rpc import encode_node_command
from app.node import sync as node_sync_module


def _greedy_reference(users: list[dict], max_batch_size: int, max_payload_bytes: int) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for user in users:
        candidate = [*current, user]
        if current and (
            len(candidate) > max_batch_size
            or len(encode_node_command("update_users", {"users": candidate})) > max_payload_bytes
        ):
            chunks.append(current)
            current = [user]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _random_users(rng: random.Random, count: int) -> list[dict]:
    alphabet = 'abcXYZ019"\\/\n\téب中\U0001f600'
    return [
        {
            "email": f"{index}.user-{rng.randrange(10**6)}",
            "id": index,
            "inbounds": [f"tag-{rng.randrange(5)}" for _ in range(rng.randrange(4))],
            "proxy": "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 400))),
        }
        for index in range(count)
    ]


@pytest.mark.parametrize("seed", range(6))
def test_user_batches_match_exact_wire_sizing(monkeypatch: pytest.MonkeyPatch, seed: int):
    rng = random.Random(seed)
    users = _random_users(rng, 120)
    max_batch_size = rng.choice([1, 3, 7, 50, 500])
    max_payload_bytes = rng.choice([1024, 1500, 4096, 20000])
    monkeypatch.setattr(node_sync_module.nats_settings, "node_update_users_batch_size", max_batch_size)
    monkeypatch.setattr(node_sync_module.nats_settings, "node_command_max_payload_bytes", max_payload_bytes)

    chunks = node_sync_module._chunk_serialized_users_for_nats(users)

    assert chunks == _greedy_reference(users, max_batch_size, max_payload_bytes)
    assert [user for chunk in chunks for user in chunk] == users
    for chunk in chunks:
        assert len(chunk) <= max_batch_size
        if len(chunk) > 1:
            assert len(encode_node_command("update_users", {"users": chunk})) <= max_payload_bytes


def test_user_batches_are_sized_without_reencoding_the_growing_batch(monkeypatch: pytest.MonkeyPatch):
    users = [{"email": f"user-{index}", "proxy": "x" * 200} for index in range(400)]
    monkeypatch.setattr(node_sync_module.nats_settings, "node_update_users_batch_size", 400)
    monkeypatch.setattr(node_sync_module.nats_settings, "node_command_max_payload_bytes", 10**8)
    encoded_bytes = 0
    real_encode = node_rpc.encode_node_command

    def counting_encode(action: str, payload: dict) -> bytes:
        nonlocal encoded_bytes
        data = real_encode(action, payload)
        encoded_bytes += len(data)
        return data

    monkeypatch.setattr(node_sync_module, "encode_node_command", counting_encode)

    chunks = node_sync_module._chunk_serialized_users_for_nats(users)

    assert chunks == [users]
    assert encoded_bytes <= 2 * len(real_encode("update_users", {"users": users}))


def test_only_users_over_the_payload_limit_are_warned_about_and_sent_alone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(node_sync_module.nats_settings, "node_update_users_batch_size", 100)
    monkeypatch.setattr(node_sync_module.nats_settings, "node_command_max_payload_bytes", 1024)
    warnings = []
    monkeypatch.setattr(node_sync_module.logger, "warning", lambda message, *args: warnings.append(args))
    small = {"id": 1, "email": "small@x", "data": "s"}
    big_by_email = {"id": 2, "email": "big@x", "data": "b" * 2000}
    big_by_id = {"id": 3, "data": "c" * 2000}
    tail = {"id": 4, "email": "tail@x", "data": "t"}

    chunks = node_sync_module._chunk_serialized_users_for_nats([small, big_by_email, big_by_id, tail])

    assert chunks == [[small], [big_by_email], [big_by_id], [tail]]
    assert warnings == [("big@x",), (3,)]
