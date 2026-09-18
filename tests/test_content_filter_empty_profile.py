import pytest

from app.fork.content_filter import service
from app.fork.content_filter.rules import build_rules


class _Profile:
    def __init__(self, name, categories=None, allow_list=None, block_list=None, strict_mode=False):
        self.name = name
        self.categories = list(categories or [])
        self.allow_list = list(allow_list or [])
        self.block_list = list(block_list or [])
        self.strict_mode = strict_mode


class _Assignment:
    def __init__(self, profile, inbound_tag="spof 401", node_id=None):
        self.id = 8
        self.inbound_tag = inbound_tag
        self.node_id = node_id
        self.is_enabled = True
        self.enforced = False
        self.last_error = None
        self.last_checked_at = None
        self.applied_digest = None
        self.profile = profile


def test_a_profile_with_nothing_in_it_is_reported_as_empty():
    assignment = _Assignment(_Profile("تبلیغات"))

    reason = service.nothing_to_enforce(assignment)

    assert reason is not None
    assert "تبلیغات" in reason
    assert "nothing to apply" in reason


def test_strict_mode_alone_does_not_make_an_empty_profile_enforceable():
    assignment = _Assignment(_Profile("تبلیغات", strict_mode=True))

    assert service.nothing_to_enforce(assignment) is not None


def test_a_strict_empty_profile_never_builds_a_catch_all_block():
    built = build_rules(
        assignment_id=8,
        inbound_tags=["spof 401"],
        categories=[],
        allow_list=[],
        block_list=[],
        strict_mode=True,
    )

    assert built == []


def test_a_profile_that_blocks_something_is_not_reported_as_empty():
    assignment = _Assignment(_Profile("ads", block_list=["ads.example"]))

    assert service.nothing_to_enforce(assignment) is None


@pytest.mark.parametrize(
    "profile",
    [
        _Profile("by category", categories=["adult"]),
        _Profile("by block list", block_list=["ads.example"]),
        _Profile("by allow list", allow_list=["ok.example"]),
    ],
)
def test_any_one_populated_list_is_enough(profile):
    assert service.nothing_to_enforce(_Assignment(profile)) is None


@pytest.mark.asyncio
async def test_applying_an_empty_profile_refuses_before_any_node_is_touched(monkeypatch: pytest.MonkeyPatch):
    assignment = _Assignment(_Profile("تبلیغات"))
    touched: list[str] = []

    async def nodes_for_assignment(db, _assignment):
        touched.append("nodes_for_assignment")
        return [10, 20]

    async def record(db, _assignment, enforced, problems):
        _assignment.enforced = enforced
        _assignment.last_error = "; ".join(problems)

    monkeypatch.setattr(service, "nodes_for_assignment", nodes_for_assignment)
    monkeypatch.setattr(service, "_record", record)

    with pytest.raises(service.EnforcementError) as raised:
        await service.apply_assignment(object(), assignment, admin=None)

    assert raised.value.code == 409
    assert "تبلیغات" in raised.value.detail
    assert touched == []
    assert "nothing to apply" in assignment.last_error


@pytest.mark.asyncio
async def test_a_node_is_not_blamed_for_a_profile_that_is_empty(monkeypatch: pytest.MonkeyPatch):
    assignment = _Assignment(_Profile("تبلیغات"))

    async def rules_for_node(db, _assignment, node_id):
        return []

    async def unreachable_cores(db, _assignment, node_id):
        return []

    monkeypatch.setattr(service, "rules_for_node", rules_for_node)
    monkeypatch.setattr(service, "unreachable_cores", unreachable_cores)

    problems = await service._node_problems(object(), assignment, 10)

    assert problems == [service.NOTHING_TO_ENFORCE.format(name="تبلیغات")]
    assert service.NO_FILTERABLE_ENDPOINT not in problems[0]


@pytest.mark.asyncio
async def test_a_node_that_really_cannot_carry_rules_is_still_named(monkeypatch: pytest.MonkeyPatch):
    assignment = _Assignment(_Profile("ads", block_list=["ads.example"]))

    async def rules_for_node(db, _assignment, node_id):
        return []

    async def unreachable_cores(db, _assignment, node_id):
        return []

    monkeypatch.setattr(service, "rules_for_node", rules_for_node)
    monkeypatch.setattr(service, "unreachable_cores", unreachable_cores)

    problems = await service._node_problems(object(), assignment, 10)

    assert problems == [f"node 10: {service.NO_FILTERABLE_ENDPOINT}"]


def test_strict_mode_with_an_empty_allow_list_is_refused_as_an_outage():
    assignment = _Assignment(_Profile("تبلیغات", categories=["adult"], strict_mode=True))

    reason = service.blocks_everything(assignment)

    assert reason is not None
    assert "تبلیغات" in reason
    assert "cut every connection" in reason


def test_strict_mode_with_an_allow_list_is_allowed():
    assignment = _Assignment(_Profile("ads", categories=["adult"], allow_list=["bank.example"], strict_mode=True))

    assert service.blocks_everything(assignment) is None


def test_a_profile_without_strict_mode_is_never_called_an_outage():
    assignment = _Assignment(_Profile("ads", categories=["adult"], strict_mode=False))

    assert service.blocks_everything(assignment) is None


def test_a_strict_profile_with_nothing_in_it_is_reported_as_empty_not_as_an_outage():
    assignment = _Assignment(_Profile("تبلیغات", strict_mode=True))

    assert service.blocks_everything(assignment) is None
    assert service.nothing_to_enforce(assignment) is not None


@pytest.mark.asyncio
async def test_applying_a_strict_profile_without_an_allow_list_never_reaches_a_node(monkeypatch: pytest.MonkeyPatch):
    assignment = _Assignment(_Profile("تبلیغات", categories=["adult"], strict_mode=True))
    touched: list[str] = []

    async def nodes_for_assignment(db, _assignment):
        touched.append("nodes")
        return [10]

    async def record(db, _assignment, enforced, problems):
        _assignment.enforced = enforced
        _assignment.last_error = "; ".join(problems)

    monkeypatch.setattr(service, "nodes_for_assignment", nodes_for_assignment)
    monkeypatch.setattr(service, "_record", record)

    with pytest.raises(service.EnforcementError) as raised:
        await service.apply_assignment(object(), assignment, admin=None)

    assert raised.value.code == 409
    assert "cut every connection" in raised.value.detail
    assert touched == []
