import pytest

from app.fork.content_filter import service


class _Rows:
    def scalars(self):
        return self

    def all(self):
        return []


class _FakeDB:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def execute(self, statement):
        return _Rows()

    async def get(self, model, pk):
        return None


class _Assignment:
    def __init__(self):
        self.id = 1
        self.enforced = True
        self.last_error = None
        self.last_checked_at = None
        self.applied_digest = None
        self.inbound_tag = "in"
        self.node_id = 1


@pytest.mark.asyncio
async def test_a_core_is_never_rewritten_when_no_node_can_accept_rules(monkeypatch: pytest.MonkeyPatch):
    persisted = []

    async def nodes(db, assignment):
        return [1, 2]

    async def refuses(node_id):
        raise service.EnforcementError("this node's core does not publish RoutingService", code=409)

    async def persist(db, core_id, admin, allow_restart=False, advisories=None):
        persisted.append(core_id)
        return True

    monkeypatch.setattr(service, "nodes_for_assignment", nodes)
    monkeypatch.setattr(service, "live_rules", refuses)
    monkeypatch.setattr(service, "persist_core_rules", persist)

    assignment = _Assignment()
    db = _FakeDB()

    with pytest.raises(service.EnforcementError) as refused:
        await service.apply_assignment(db, assignment, admin=object())

    assert refused.value.code == 409
    assert persisted == []
    assert assignment.enforced is False
    assert "RoutingService" in assignment.last_error
    assert db.commits == 1


@pytest.mark.asyncio
async def test_a_reachable_node_still_gets_its_core_rewritten(monkeypatch: pytest.MonkeyPatch):
    persisted = []
    pushed = []

    async def nodes(db, assignment):
        return [5]

    async def accepts(node_id):
        return [{"ruleTag": "pgcf-1-cat", "outboundTag": "BLOCK"}]

    async def persist(db, core_id, admin, allow_restart=False, advisories=None):
        persisted.append(core_id)
        return True

    async def push(db, node_id, advisories=None):
        pushed.append(node_id)

    def rules(assignment):
        return [{"ruleTag": "pgcf-1-cat", "outboundTag": "BLOCK"}]

    class _Node:
        id = 5
        core_config_id = 9

    class _Core:
        def __init__(self):
            self.type = "xray"
            self.config = {
                "inbounds": [{"tag": "in", "protocol": "vless"}],
                "outbounds": [{"tag": "BLOCK", "protocol": "blackhole"}, {"tag": "DIRECT", "protocol": "freedom"}],
            }

    async def get_node(model, pk):
        return _Node()

    async def core_of(db, core_id):
        return _Core()

    monkeypatch.setattr(service, "get_core_config_by_id", core_of)
    monkeypatch.setattr(service, "nodes_for_assignment", nodes)
    monkeypatch.setattr(service, "live_rules", accepts)
    monkeypatch.setattr(service, "persist_core_rules", persist)
    monkeypatch.setattr(service, "push_live", push)
    monkeypatch.setattr(service, "assignment_rules", rules)

    db = _FakeDB()
    db.get = get_node
    assignment = _Assignment()

    result = await service.apply_assignment(db, assignment, admin=object())

    assert persisted == [9]
    assert pushed == [5]
    assert assignment.enforced is True
    assert result is not None
