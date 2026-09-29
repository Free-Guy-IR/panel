import asyncio

from app.node import node_manager

seen_loops = []


async def test_one_test_loop_takes_the_node_manager_lock():
    seen_loops.append(asyncio.get_running_loop())
    assert await node_manager.get_node(-1) is None


async def test_the_next_test_loop_takes_the_node_manager_lock_as_well():
    seen_loops.append(asyncio.get_running_loop())
    assert seen_loops[0] is not seen_loops[-1]
    assert await node_manager.get_node(-1) is None
