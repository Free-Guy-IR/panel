import logging

import pytest

from app.fork import node_push_audit


@pytest.fixture(autouse=True)
def _clean_state():
    node_push_audit.reset()
    yield
    node_push_audit.reset()


def test_the_first_push_is_never_an_alert(caplog):
    caplog.set_level(logging.WARNING, logger="node-push-audit")

    node_push_audit.record_user_push(1, 4000)

    assert caplog.text == ""
    assert node_push_audit.last_pushed_count(1) == 4000


def test_a_large_drop_is_reported_with_both_counts(caplog):
    node_push_audit.record_user_push(1, 4000)
    caplog.set_level(logging.WARNING, logger="node-push-audit")

    node_push_audit.record_user_push(1, 10)

    assert "4000" in caplog.text
    assert "10" in caplog.text
    assert "lose service" in caplog.text


def test_a_total_wipe_is_reported(caplog):
    node_push_audit.record_user_push(7, 4432)
    caplog.set_level(logging.WARNING, logger="node-push-audit")

    node_push_audit.record_user_push(7, 0)

    assert "4432" in caplog.text
    assert "100.0%" in caplog.text


def test_ordinary_churn_is_not_reported(caplog):
    node_push_audit.record_user_push(1, 4000)
    caplog.set_level(logging.WARNING, logger="node-push-audit")

    node_push_audit.record_user_push(1, 3950)

    assert caplog.text == ""


def test_growth_is_never_reported(caplog):
    node_push_audit.record_user_push(1, 100)
    caplog.set_level(logging.WARNING, logger="node-push-audit")

    node_push_audit.record_user_push(1, 5000)

    assert caplog.text == ""


def test_a_tiny_node_does_not_cry_wolf(caplog):
    node_push_audit.record_user_push(1, 5)
    caplog.set_level(logging.WARNING, logger="node-push-audit")

    node_push_audit.record_user_push(1, 0)

    assert caplog.text == ""


def test_nodes_are_tracked_independently(caplog):
    node_push_audit.record_user_push(1, 4000)
    node_push_audit.record_user_push(2, 4000)
    caplog.set_level(logging.WARNING, logger="node-push-audit")

    node_push_audit.record_user_push(1, 10)

    assert caplog.text.count("user push fell") == 1
    assert node_push_audit.last_pushed_count(2) == 4000


def test_forgetting_a_node_clears_its_baseline(caplog):
    node_push_audit.record_user_push(1, 4000)
    node_push_audit.forget_node(1)
    caplog.set_level(logging.WARNING, logger="node-push-audit")

    node_push_audit.record_user_push(1, 10)

    assert caplog.text == ""
    assert node_push_audit.last_pushed_count(1) == 10


def test_the_full_snapshot_push_is_audited_before_it_reaches_the_node():
    import inspect

    from app.node import NodeManager

    source = inspect.getsource(NodeManager.sync_full)

    assert "record_user_push(node_id, len(users))" in source
    assert source.index("record_user_push") < source.index("await node.sync_users")


def test_the_incremental_batch_path_is_deliberately_not_audited():
    import inspect

    from app.node import NodeManager

    source = inspect.getsource(NodeManager._sync_users_to_node)

    assert "record_user_push" not in source
