import ast
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, AdminRole, Base, Group, User, UserTemplate
from app.models.admin import AdminDetails, AdminRoleData
from app.models.proxy import ProxyTable
from app.telegram.handlers.admin import user as handlers
from app.telegram.keyboards.user import UserPanel, UserPanelAction
from app.telegram.utils import forms
from app.telegram.utils.texts import Message as Texts

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"
LOADERS = {"get_validated_user", "get_validated_user_by_id"}

OWNER = AdminDetails(id=1, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))


def _role_admin(admin_id: int, users: dict) -> AdminDetails:
    return AdminDetails(
        id=admin_id,
        username=f"admin{admin_id}",
        role=AdminRoleData(id=3, name="reseller", is_owner=False, permissions={"users": users}),
    )


RESELLER = _role_admin(2, {"create": True, "read": {"scope": 1}})
CREATE_ONLY = _role_admin(2, {"create": True})


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        for role_id, name, is_owner in ((1, "owner", True), (3, "reseller", False)):
            role = AdminRole(name=name, is_owner=is_owner, permissions={}, limits={}, features={}, access={})
            role.id = role_id
            session.add(role)
        await session.flush()
        for admin_id, username, role_id in ((1, "owner", 1), (2, "admin2", 3), (3, "other", 3)):
            admin = Admin(username=username, hashed_password="x", role_id=role_id)
            admin.id = admin_id
            session.add(admin)
        await session.flush()
        group = Group(name="g", inbounds=[])
        session.add(group)
        await session.flush()
        for user_id, username, admin_id in ((10, "taken", 3), (11, "pre_both_suf", 3)):
            user = User(username=username, admin_id=admin_id, proxy_settings=ProxyTable().dict(no_obj=True))
            user.id = user_id
            user.groups = [group]
            session.add(user)
        template = UserTemplate(
            name="t", username_prefix="pre_", username_suffix="_suf", extra_settings=None, groups=[group], data_limit=0
        )
        template.id = 5
        session.add(template)
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def _reply() -> SimpleNamespace:
    return SimpleNamespace(message_id=99)


def _message(text: str) -> MagicMock:
    event = MagicMock()
    event.__class__ = Message
    event.text = text
    event.message_id = 1
    event.chat.id = 1
    event.bot.delete_messages = AsyncMock()
    event.reply = AsyncMock(return_value=_reply())
    event.answer = AsyncMock(return_value=_reply())
    return event


def _callback() -> MagicMock:
    event = MagicMock()
    event.__class__ = CallbackQuery
    event.message.message_id = 1
    event.message.chat.id = 1
    event.message.answer = AsyncMock(return_value=_reply())
    event.bot.delete_messages = AsyncMock()
    event.answer = AsyncMock()
    return event


def _replied_text(event: MagicMock) -> str:
    return event.reply.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [OWNER, RESELLER, CREATE_ONLY], ids=["owner", "own-scope reseller", "create-only"])
async def test_the_create_flow_refuses_a_username_taken_in_any_tenant(db, state, admin):
    await state.set_state(forms.CreateUser.username)
    event = _message("taken")

    await handlers.process_username(event, state, db, admin)

    assert _replied_text(event) == Texts.username_already_exist
    assert await state.get_state() == forms.CreateUser.username.state


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [OWNER, RESELLER, CREATE_ONLY], ids=["owner", "own-scope reseller", "create-only"])
async def test_the_create_flow_moves_on_with_a_free_username(db, state, admin):
    await state.set_state(forms.CreateUser.username)
    event = _message("fresh_name")

    await handlers.process_username(event, state, db, admin)

    event.reply.assert_not_awaited()
    assert await state.get_state() == forms.CreateUser.data_limit.state
    assert await state.get_value("username") == "fresh_name"


class _Created(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [OWNER, RESELLER, CREATE_ONLY], ids=["owner", "own-scope reseller", "create-only"])
async def test_the_template_flow_checks_the_name_the_template_will_create(monkeypatch, db, state, admin):
    await state.update_data(template_id=5)
    create = AsyncMock(side_effect=_Created)
    monkeypatch.setattr(handlers.user_operations, "create_user_from_template", create)

    taken = _message("both")
    await handlers.create_user_from_template_choose(taken, state, db, admin)
    assert _replied_text(taken) == Texts.username_already_exist
    create.assert_not_awaited()

    free = _message("fresh")
    with pytest.raises(_Created):
        await handlers.create_user_from_template_choose(free, state, db, admin)
    assert create.await_args.args[1].username == "fresh"


@pytest.mark.asyncio
async def test_a_template_name_taken_after_the_check_gets_a_clean_reply(monkeypatch, db, state):
    await state.update_data(template_id=5)
    monkeypatch.setattr(
        handlers.user_operations,
        "create_user_from_template",
        AsyncMock(side_effect=ValueError("User already exists")),
    )
    message = _message("racing")
    callback = _callback()

    await handlers.create_user_from_template_choose(message, state, db, OWNER)
    await handlers.create_user_from_template_choose(callback, state, db, OWNER)

    assert _replied_text(message) == "❌ User already exists"
    assert callback.answer.await_args.args[0] == "❌ User already exists"
    assert await state.get_value("template_id") == 5


@pytest.mark.asyncio
async def test_a_plain_create_that_fails_at_the_last_step_gets_a_clean_reply(monkeypatch, db, state):
    await state.update_data(
        username="racing",
        data_limit=1,
        duration=0,
        status="active",
        group_ids=[1],
        messages_to_delete=[],
    )
    monkeypatch.setattr(
        handlers.user_operations, "create_user", AsyncMock(side_effect=ValueError("User already exists"))
    )
    event = _callback()

    await handlers.process_done(event, db, OWNER, state)

    assert event.answer.await_args.args[0] == "❌ User already exists"


@pytest.fixture
def stub_user_rendering(monkeypatch):
    rendered = SimpleNamespace(subscription_url="https://sub/x", username="taken")
    monkeypatch.setattr(handlers.user_operations, "validate_user", AsyncMock(return_value=rendered))
    monkeypatch.setattr(handlers.subscription_operations, "validated_user", AsyncMock(return_value=rendered))
    monkeypatch.setattr(handlers.subscription_operations, "fetch_config", AsyncMock(return_value=("vless://x", None)))
    monkeypatch.setattr(handlers.Texts, "client_user_details", lambda user: "details")
    qr = AsyncMock()
    monkeypatch.setattr(handlers, "send_subscription_qr", qr)
    return qr


@pytest.mark.asyncio
async def test_the_qr_and_links_callbacks_load_a_user_the_admin_may_read(db, stub_user_rendering):
    qr_event, links_event = _callback(), _callback()

    await handlers.get_subscription_qr(
        qr_event, db, OWNER, UserPanel.Callback(user_id=10, action=UserPanelAction.subscription_qr)
    )
    await handlers.get_v2ray_links(
        links_event, db, OWNER, UserPanel.Callback(user_id=10, action=UserPanelAction.v2ray_links)
    )

    stub_user_rendering.assert_awaited_once()
    assert links_event.message.answer.await_args_list[-1].args[0] == "<pre>vless://x</pre>"


@pytest.mark.asyncio
async def test_the_qr_and_links_callbacks_hide_a_user_outside_the_read_scope(db, stub_user_rendering):
    qr_event, links_event = _callback(), _callback()

    await handlers.get_subscription_qr(
        qr_event, db, RESELLER, UserPanel.Callback(user_id=10, action=UserPanelAction.subscription_qr)
    )
    await handlers.get_v2ray_links(
        links_event, db, RESELLER, UserPanel.Callback(user_id=10, action=UserPanelAction.v2ray_links)
    )

    stub_user_rendering.assert_not_awaited()
    assert qr_event.answer.await_args.args[0] == Texts.user_not_found
    assert links_event.answer.await_args.args[0] == "User not found"


def test_every_user_loader_call_in_the_app_names_its_action():
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LOADERS
                and not any(keyword.arg in ("scope_action", None) for keyword in node.keywords)
            ):
                offenders.append(f"{path.relative_to(APP_DIR.parent)}:{node.lineno}")

    assert offenders == [], f"loader calls without an explicit scope_action: {offenders}"
