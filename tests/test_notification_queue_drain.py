import asyncio

import pytest

from app.notification.nats_queue import InMemoryNotificationQueue, NatsNotificationQueue


async def test_drain_hands_back_queued_delayed_and_released_items():
    queue = InMemoryNotificationQueue(maxsize=3)
    for number in range(3):
        await queue.enqueue({"n": number})
    delivery = await queue.dequeue(timeout=0)
    await delivery.release(delay=30)

    drained = queue.drain_nowait()

    assert sorted(item["n"] for item in drained) == [0, 1, 2]
    assert queue.drain_nowait() == []
    assert await queue.dequeue(timeout=0) is None


async def test_drain_frees_the_count_and_byte_capacity_the_items_held():
    queue = InMemoryNotificationQueue(maxsize=2, max_bytes=20)
    await queue.enqueue({"n": 0})
    await queue.enqueue({"n": 1})
    with pytest.raises(asyncio.QueueFull):
        await queue.enqueue({"n": 2})

    assert len(queue.drain_nowait()) == 2

    await queue.enqueue({"n": 3})
    await queue.enqueue({"n": 4})
    with pytest.raises(asyncio.QueueFull):
        await queue.enqueue({"n": 5})


async def test_drain_frees_the_retry_reservation_of_a_released_delivery():
    queue = InMemoryNotificationQueue(maxsize=10, max_bytes=200)
    await queue.enqueue({"n": 0})
    first = await queue.dequeue(timeout=0)
    await first.prepare({"n": 0, "tries": 1})
    await first.release(delay=30)

    assert queue.drain_nowait() == [{"n": 0, "tries": 1}]

    await queue.enqueue({"n": 1})
    second = await queue.dequeue(timeout=0)
    await second.prepare({"n": 1, "tries": 1})
    await second.ack()


def test_a_nats_queue_keeps_its_backlog_in_jetstream_and_drains_nothing():
    assert NatsNotificationQueue().drain_nowait() == []
